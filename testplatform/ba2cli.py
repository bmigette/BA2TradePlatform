#!/usr/bin/env python3
"""BA2 ML Test Platform CLI — wraps the FastAPI backend for scripting and LLM agents."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


# ---------------------------------------------------------------------------
# HTTP Client
# ---------------------------------------------------------------------------

def http_request(method, url, data=None, headers=None):
    """Send an HTTP request and return the parsed JSON response."""
    hdrs = headers or {}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)

    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        try:
            err_body = json.loads(exc.read())
        except Exception:
            err_body = {"error": str(exc), "status": exc.code}
        print(json.dumps(err_body, indent=2), file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(
            json.dumps(
                {"error": "Connection refused", "detail": str(exc.reason), "url": url},
                indent=2,
            ),
            file=sys.stderr,
        )
        sys.exit(1)


def api_call(args, method, path, data=None, extra_headers=None):
    """Build the full URL from args and delegate to http_request."""
    url = "http://{}:{}{}".format(args.host, args.port, path)
    headers = extra_headers or {}
    token = getattr(args, "token", None)
    if token:
        headers["Authorization"] = "Bearer {}".format(token)
    return http_request(method, url, data=data, headers=headers)


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def print_table(rows, columns):
    """Print an aligned text table.

    *columns* is a list of ``(header, key, width)`` tuples.
    """
    if not rows:
        print("(no rows)")
        return

    # Header
    parts = []
    for header, _key, width in columns:
        parts.append(header.ljust(width)[:width])
    print("  ".join(parts))

    # Separator
    parts = []
    for _header, _key, width in columns:
        parts.append("-" * width)
    print("  ".join(parts))

    # Rows
    for row in rows:
        parts = []
        for _header, key, width in columns:
            val = row.get(key) if isinstance(row, dict) else getattr(row, key, None)
            if val is None:
                display = "\u2014"
            else:
                display = str(val)
            if len(display) > width:
                display = display[: width - 1] + "\u2026"
            parts.append(display.ljust(width)[:width])
        print("  ".join(parts))


def format_output(data, human=False, table_fn=None):
    """Print *data* as JSON (default) or as a human-readable table.

    *table_fn* is an optional callable ``(data) -> None`` that renders data as
    a table.  If *human* is True and *table_fn* is provided it will be called;
    otherwise we fall back to indented JSON.
    """
    if data is None:
        data = {"status": "ok"}
    if human and table_fn is not None:
        try:
            table_fn(data)
            return
        except Exception:
            pass  # fall through to JSON
    print(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# List extraction helper
# ---------------------------------------------------------------------------

def extract_list(data):
    """Extract a list from an API response.

    Many endpoints return ``{"items": [...], "total": N}`` where the key varies
    (datasets, jobs, strategies, etc.).  This helper finds the first list value
    in a dict, or returns *data* unchanged if it is already a list.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


# ---------------------------------------------------------------------------
# JSON argument helper
# ---------------------------------------------------------------------------

def parse_json_arg(value):
    """Parse a JSON argument.

    If *value* starts with ``@``, treat the remainder as a file path and read
    JSON from that file.  Otherwise parse *value* as an inline JSON string.
    """
    if value is None:
        return None

    if value.startswith("@"):
        path = value[1:]
        try:
            with open(path, "r") as fh:
                text = fh.read()
        except FileNotFoundError:
            print(
                json.dumps({"error": "File not found", "path": path}, indent=2),
                file=sys.stderr,
            )
            sys.exit(1)
        except OSError as exc:
            print(
                json.dumps({"error": "Cannot read file", "detail": str(exc)}, indent=2),
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        text = value

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        print(
            json.dumps(
                {"error": "Invalid JSON", "detail": str(exc), "input": text[:200]},
                indent=2,
            ),
            file=sys.stderr,
        )
        sys.exit(1)


# ===================================================================
# Resource: datasets
# ===================================================================

def register_datasets_commands(subparsers):
    ds = subparsers.add_parser("datasets", help="Manage datasets")
    actions = ds.add_subparsers(dest="action")

    # list
    actions.add_parser("list", help="List all datasets")

    # get
    p = actions.add_parser("get", help="Get dataset by ID")
    p.add_argument("id", type=int)

    # create
    p = actions.add_parser("create", help="Create a new dataset")
    p.add_argument("--ticker", required=True)
    p.add_argument("--timeframe", required=True)
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--name")
    p.add_argument("--provider", default="yfinance")
    p.add_argument("--indicator-collection-id", type=int)
    p.add_argument("--labels", help="Comma-separated labels")

    # delete
    p = actions.add_parser("delete", help="Delete dataset")
    p.add_argument("id", type=int)

    # rename
    p = actions.add_parser("rename", help="Rename dataset")
    p.add_argument("id", type=int)
    p.add_argument("--name", required=True)

    # duplicate
    p = actions.add_parser("duplicate", help="Duplicate dataset")
    p.add_argument("id", type=int)
    p.add_argument("--new-ticker")
    p.add_argument("--new-name")

    # regenerate
    p = actions.add_parser("regenerate", help="Regenerate dataset data")
    p.add_argument("id", type=int)
    p.add_argument("--no-ohlcv", action="store_true", help="Skip OHLCV data")
    p.add_argument("--no-technical", action="store_true", help="Skip technical indicators")
    p.add_argument("--no-sentiment", action="store_true", help="Skip sentiment data")
    p.add_argument("--no-fundamentals", action="store_true", help="Skip fundamentals")
    p.add_argument("--no-macro", action="store_true", help="Skip macro data")

    # preview
    p = actions.add_parser("preview", help="Preview dataset")
    p.add_argument("id", type=int)

    # stats
    p = actions.add_parser("stats", help="Get dataset statistics")
    p.add_argument("id", type=int)

    # columns
    p = actions.add_parser("columns", help="List dataset columns")
    p.add_argument("id", type=int)

    # export
    p = actions.add_parser("export", help="Export dataset")
    p.add_argument("id", type=int)
    p.add_argument("--format", choices=["csv", "parquet"], default="csv")


def handle_datasets(args):
    action = getattr(args, "action", None)
    if not action:
        print("Usage: ba2cli.py datasets <action>", file=sys.stderr)
        sys.exit(1)

    if action == "list":
        data = api_call(args, "GET", "/api/datasets")
        format_output(
            data,
            human=args.human,
            table_fn=lambda d: print_table(
                extract_list(d),
                [
                    ("ID", "id", 6),
                    ("Name", "name", 28),
                    ("Ticker", "ticker", 10),
                    ("Timeframe", "timeframe", 12),
                    ("Rows", "rows_count", 8),
                    ("Status", "status", 12),
                ],
            ),
        )

    elif action == "get":
        data = api_call(args, "GET", "/api/datasets/{}".format(args.id))
        format_output(data, human=args.human)

    elif action == "create":
        body = {
            "ticker": args.ticker,
            "timeframe": args.timeframe,
            "data_provider": args.provider,
        }
        if args.start is not None:
            body["start_date"] = args.start
        if args.end is not None:
            body["end_date"] = args.end
        if args.name is not None:
            body["name"] = args.name
        if args.indicator_collection_id is not None:
            body["indicator_collection_id"] = args.indicator_collection_id
        if args.labels is not None:
            body["labels"] = [l.strip() for l in args.labels.split(",")]
        data = api_call(args, "POST", "/api/datasets", data=body)
        format_output(data, human=args.human)

    elif action == "delete":
        data = api_call(args, "DELETE", "/api/datasets/{}".format(args.id))
        format_output(data, human=args.human)

    elif action == "rename":
        data = api_call(
            args,
            "PATCH",
            "/api/datasets/{}/rename".format(args.id),
            data={"name": args.name},
        )
        format_output(data, human=args.human)

    elif action == "duplicate":
        body = {}
        if args.new_ticker is not None:
            body["new_ticker"] = args.new_ticker
        if args.new_name is not None:
            body["new_name"] = args.new_name
        data = api_call(
            args, "POST", "/api/datasets/{}/duplicate".format(args.id), data=body
        )
        format_output(data, human=args.human)

    elif action == "regenerate":
        body = {
            "ohlcv": not args.no_ohlcv,
            "technical": not args.no_technical,
            "sentiment": not args.no_sentiment,
            "fundamentals": not args.no_fundamentals,
            "macro": not args.no_macro,
        }
        data = api_call(
            args, "POST", "/api/datasets/{}/regenerate".format(args.id), data=body
        )
        format_output(data, human=args.human)

    elif action == "preview":
        data = api_call(args, "GET", "/api/datasets/{}/preview".format(args.id))
        format_output(data, human=args.human)

    elif action == "stats":
        data = api_call(args, "GET", "/api/datasets/{}/stats".format(args.id))
        format_output(data, human=args.human)

    elif action == "columns":
        data = api_call(args, "GET", "/api/datasets/{}/columns".format(args.id))
        format_output(data, human=args.human)

    elif action == "export":
        if args.format == "parquet":
            path = "/api/datasets/{}/export/parquet".format(args.id)
        else:
            path = "/api/datasets/{}/export".format(args.id)
        data = api_call(args, "GET", path)
        format_output(data, human=args.human)


# ===================================================================
# Resource: targets (target sets)
# ===================================================================

def register_targets_commands(subparsers):
    ts = subparsers.add_parser("targets", help="Manage target sets")
    actions = ts.add_subparsers(dest="action")

    # list
    actions.add_parser("list", help="List all target sets")

    # get
    p = actions.add_parser("get", help="Get target set by ID")
    p.add_argument("id", type=int)

    # create
    p = actions.add_parser("create", help="Create a target set")
    p.add_argument("--name", required=True)
    p.add_argument("--description")
    p.add_argument("--targets", required=True, help="JSON string or @file")

    # update
    p = actions.add_parser("update", help="Update a target set")
    p.add_argument("id", type=int)
    p.add_argument("--name")
    p.add_argument("--description")
    p.add_argument("--targets", help="JSON string or @file")

    # delete
    p = actions.add_parser("delete", help="Delete a target set")
    p.add_argument("id", type=int)

    # preview
    p = actions.add_parser("preview", help="Preview targets on a dataset")
    p.add_argument("--dataset-id", required=True, type=int)
    p.add_argument("--targets", required=True, help="JSON string or @file")


def handle_targets(args):
    action = getattr(args, "action", None)
    if not action:
        print("Usage: ba2cli.py targets <action>", file=sys.stderr)
        sys.exit(1)

    if action == "list":
        data = api_call(args, "GET", "/api/target-sets")
        format_output(data, human=args.human)

    elif action == "get":
        data = api_call(args, "GET", "/api/target-sets/{}".format(args.id))
        format_output(data, human=args.human)

    elif action == "create":
        body = {
            "name": args.name,
            "targets": parse_json_arg(args.targets),
        }
        if args.description is not None:
            body["description"] = args.description
        data = api_call(args, "POST", "/api/target-sets", data=body)
        format_output(data, human=args.human)

    elif action == "update":
        body = {}
        if args.name is not None:
            body["name"] = args.name
        if args.description is not None:
            body["description"] = args.description
        if args.targets is not None:
            body["targets"] = parse_json_arg(args.targets)
        data = api_call(args, "PUT", "/api/target-sets/{}".format(args.id), data=body)
        format_output(data, human=args.human)

    elif action == "delete":
        data = api_call(args, "DELETE", "/api/target-sets/{}".format(args.id))
        format_output(data, human=args.human)

    elif action == "preview":
        body = {
            "targets": parse_json_arg(args.targets),
        }
        data = api_call(
            args,
            "POST",
            "/api/datasets/{}/preview-targets".format(args.dataset_id),
            data=body,
        )
        format_output(data, human=args.human)


# ===================================================================
# Resource: indicators (indicator collections)
# ===================================================================

def register_indicators_commands(subparsers):
    ic = subparsers.add_parser("indicators", help="Manage indicator collections")
    actions = ic.add_subparsers(dest="action")

    # list
    actions.add_parser("list", help="List all indicator collections")

    # get
    p = actions.add_parser("get", help="Get indicator collection by ID")
    p.add_argument("id", type=int)

    # create
    p = actions.add_parser("create", help="Create an indicator collection")
    p.add_argument("--name", required=True)
    p.add_argument("--description")
    p.add_argument("--indicators", required=True, help="JSON string or @file")

    # update
    p = actions.add_parser("update", help="Update an indicator collection")
    p.add_argument("id", type=int)
    p.add_argument("--name")
    p.add_argument("--description")
    p.add_argument("--indicators", help="JSON string or @file")

    # delete
    p = actions.add_parser("delete", help="Delete an indicator collection")
    p.add_argument("id", type=int)

    # supported
    actions.add_parser("supported", help="List supported indicator types")


def handle_indicators(args):
    action = getattr(args, "action", None)
    if not action:
        print("Usage: ba2cli.py indicators <action>", file=sys.stderr)
        sys.exit(1)

    if action == "list":
        data = api_call(args, "GET", "/api/indicator-collections")
        format_output(data, human=args.human)

    elif action == "get":
        data = api_call(
            args, "GET", "/api/indicator-collections/{}".format(args.id)
        )
        format_output(data, human=args.human)

    elif action == "create":
        body = {
            "name": args.name,
            "indicators": parse_json_arg(args.indicators),
        }
        if args.description is not None:
            body["description"] = args.description
        data = api_call(args, "POST", "/api/indicator-collections", data=body)
        format_output(data, human=args.human)

    elif action == "update":
        body = {}
        if args.name is not None:
            body["name"] = args.name
        if args.description is not None:
            body["description"] = args.description
        if args.indicators is not None:
            body["indicators"] = parse_json_arg(args.indicators)
        data = api_call(
            args,
            "PUT",
            "/api/indicator-collections/{}".format(args.id),
            data=body,
        )
        format_output(data, human=args.human)

    elif action == "delete":
        data = api_call(
            args, "DELETE", "/api/indicator-collections/{}".format(args.id)
        )
        format_output(data, human=args.human)

    elif action == "supported":
        data = api_call(args, "GET", "/api/indicator-collections/supported-indicators")
        format_output(data, human=args.human)


# ===================================================================
# Resource: jobs
# ===================================================================

def register_jobs_commands(subparsers):
    js = subparsers.add_parser("jobs", help="Manage training jobs")
    actions = js.add_subparsers(dest="action")

    # list
    actions.add_parser("list", help="List all jobs")

    # get
    p = actions.add_parser("get", help="Get job by ID")
    p.add_argument("id")

    # create
    p = actions.add_parser("create", help="Create a new training job")
    p.add_argument("--dataset-id", type=int)
    p.add_argument("--dataset-ids", help="Comma-separated dataset IDs")
    p.add_argument("--model-types", required=True, help="Comma-separated model types (e.g. LSTM,GRU)")
    p.add_argument("--targets", required=True, help="JSON string or @file")
    p.add_argument("--train-test-split", type=int, required=True, help="e.g. 80")
    p.add_argument("--prediction-horizon", type=int, default=3)
    p.add_argument("--prediction-modes", default="shift", help="Comma-separated modes")
    p.add_argument("--param-ranges", required=True, help="JSON string or @file")
    p.add_argument("--genetic-config", help="JSON string or @file")
    p.add_argument("--metrics-config", help="JSON string or @file")
    p.add_argument("--training-date-range", help="JSON string")
    p.add_argument("--cross-validation", help="JSON string or @file")
    p.add_argument("--job-type", default="classification")

    # prepare
    p = actions.add_parser("prepare", help="Calculate targets and get recommendations")
    p.add_argument("--dataset-id", required=True, type=int)
    p.add_argument("--targets", required=True, help="JSON string or @file")

    # delete
    p = actions.add_parser("delete", help="Delete a job")
    p.add_argument("id")

    # progress
    p = actions.add_parser("progress", help="Get job progress")
    p.add_argument("id")

    # pause
    p = actions.add_parser("pause", help="Pause a running job")
    p.add_argument("id")

    # resume
    p = actions.add_parser("resume", help="Resume a paused job")
    p.add_argument("id")

    # cancel
    p = actions.add_parser("cancel", help="Cancel a job")
    p.add_argument("id")

    # logs
    p = actions.add_parser("logs", help="Get job logs")
    p.add_argument("id")

    # generations
    p = actions.add_parser("generations", help="Get job generation history")
    p.add_argument("id")

    # individuals
    p = actions.add_parser("individuals", help="Get job individuals")
    p.add_argument("id")

    # elite-models
    p = actions.add_parser("elite-models", help="Get elite models from a job")
    p.add_argument("id")

    # save-model
    p = actions.add_parser("save-model", help="Save an elite model to inventory")
    p.add_argument("id")
    p.add_argument("--rank", required=True, type=int)
    p.add_argument("--name", help="Optional name for the saved model")


def _compute_recommendations(result):
    """Compute metric and loss recommendations from target calculation results."""
    targets_data = result if isinstance(result, list) else result.get("targets", [])
    if not targets_data:
        return {"metric": "accuracy", "loss_function": "cross_entropy"}

    positives = []
    for t in targets_data:
        # Try several likely key names for positive percentage
        pct = None
        for key in ("positive_pct", "positive_percentage", "positivePercentage", "pct_positive"):
            pct = t.get(key) if isinstance(t, dict) else None
            if pct is not None:
                break
        # Also try computing from counts
        if pct is None and isinstance(t, dict):
            total = t.get("total", t.get("count", 0))
            pos = t.get("positive", t.get("positive_count", 0))
            if total and total > 0:
                pct = (pos / total) * 100
        if pct is not None:
            positives.append(float(pct))

    if not positives:
        return {"metric": "accuracy", "loss_function": "cross_entropy"}

    avg_positive = sum(positives) / len(positives)

    # Metric recommendation
    if 42 <= avg_positive <= 58:
        metric = "accuracy"
    elif 35 <= avg_positive <= 65:
        metric = "balanced_accuracy"
    else:
        metric = "f1_score"

    # Loss recommendation
    if any(p < 20 or p > 80 for p in positives):
        loss = "focal_loss"
    else:
        loss = "cross_entropy"

    return {"metric": metric, "loss_function": loss}


def handle_jobs(args):
    action = getattr(args, "action", None)
    if not action:
        print("Usage: ba2cli.py jobs <action>", file=sys.stderr)
        sys.exit(1)

    if action == "list":
        data = api_call(args, "GET", "/api/jobs")
        format_output(
            data,
            human=args.human,
            table_fn=lambda d: print_table(
                extract_list(d),
                [
                    ("ID", "id", 10),
                    ("Status", "status", 14),
                    ("Models", "selectedModels", 20),
                    ("Generation", "currentGeneration", 12),
                    ("Best Fitness", "bestFitness", 14),
                ],
            ),
        )

    elif action == "get":
        data = api_call(args, "GET", "/api/jobs/{}".format(args.id))
        format_output(data, human=args.human)

    elif action == "create":
        body = {
            "jobType": args.job_type,
            "selectedModels": args.model_types.split(","),
            "predictionTargets": parse_json_arg(args.targets),
            "trainTestSplit": args.train_test_split,
            "predictionHorizon": args.prediction_horizon,
            "parameterRanges": parse_json_arg(args.param_ranges),
        }
        if args.dataset_id:
            body["datasetId"] = args.dataset_id
        if args.dataset_ids:
            body["datasetIds"] = [int(x) for x in args.dataset_ids.split(",")]
        if args.prediction_modes:
            body["predictionModes"] = args.prediction_modes.split(",")
        if args.genetic_config:
            body["geneticConfig"] = parse_json_arg(args.genetic_config)
        if args.metrics_config:
            body["metricsConfig"] = parse_json_arg(args.metrics_config)
        if args.training_date_range:
            body["trainingDateRange"] = parse_json_arg(args.training_date_range)
        if args.cross_validation:
            body["crossValidation"] = parse_json_arg(args.cross_validation)

        data = api_call(args, "POST", "/api/jobs", data=body)
        format_output(data, human=args.human)

    elif action == "prepare":
        targets = parse_json_arg(args.targets)
        data = api_call(
            args,
            "POST",
            "/api/ml/datasets/{}/calculate-targets".format(args.dataset_id),
            data=targets,
        )
        if data is None:
            data = {}
        data["recommendations"] = _compute_recommendations(data)
        format_output(data, human=args.human)

    elif action == "delete":
        data = api_call(args, "DELETE", "/api/jobs/{}".format(args.id))
        format_output(data, human=args.human)

    elif action == "progress":
        data = api_call(args, "GET", "/api/jobs/{}/progress".format(args.id))
        format_output(data, human=args.human)

    elif action == "pause":
        data = api_call(args, "POST", "/api/jobs/{}/pause".format(args.id))
        format_output(data, human=args.human)

    elif action == "resume":
        data = api_call(args, "POST", "/api/jobs/{}/resume".format(args.id))
        format_output(data, human=args.human)

    elif action == "cancel":
        data = api_call(args, "POST", "/api/jobs/{}/cancel".format(args.id))
        format_output(data, human=args.human)

    elif action == "logs":
        data = api_call(args, "GET", "/api/jobs/{}/logs".format(args.id))
        format_output(data, human=args.human)

    elif action == "generations":
        data = api_call(args, "GET", "/api/jobs/{}/generations".format(args.id))
        format_output(data, human=args.human)

    elif action == "individuals":
        data = api_call(args, "GET", "/api/jobs/{}/individuals".format(args.id))
        format_output(data, human=args.human)

    elif action == "elite-models":
        data = api_call(args, "GET", "/api/jobs/{}/elite-models".format(args.id))
        format_output(data, human=args.human)

    elif action == "save-model":
        body = {}
        if args.name is not None:
            body["name"] = args.name
        data = api_call(
            args,
            "POST",
            "/api/jobs/{}/elite-models/{}/save-to-inventory".format(args.id, args.rank),
            data=body,
        )
        format_output(data, human=args.human)


# ===================================================================
# Resource: profiles
# ===================================================================

def register_profiles_commands(subparsers):
    ps = subparsers.add_parser("profiles", help="Manage job profiles")
    actions = ps.add_subparsers(dest="action")

    # list
    actions.add_parser("list", help="List all profiles")

    # get
    p = actions.add_parser("get", help="Get profile by ID")
    p.add_argument("id", type=int)

    # create
    p = actions.add_parser("create", help="Create a profile")
    p.add_argument("--name", required=True)
    p.add_argument("--description")
    p.add_argument("--json", dest="json_body", help="JSON string or @file for full profile body")

    # update
    p = actions.add_parser("update", help="Update a profile")
    p.add_argument("id", type=int)
    p.add_argument("--name")
    p.add_argument("--description")
    p.add_argument("--json", dest="json_body", help="JSON string or @file for full profile body")

    # delete
    p = actions.add_parser("delete", help="Delete a profile")
    p.add_argument("id", type=int)

    # apply
    p = actions.add_parser("apply", help="Apply profile to a dataset")
    p.add_argument("id", type=int)
    p.add_argument("--dataset-id", required=True, type=int)

    # export
    p = actions.add_parser("export", help="Export a profile")
    p.add_argument("id", type=int)

    # import
    p = actions.add_parser("import", help="Import a profile from file")
    p.add_argument("--file", required=True, help="Path to JSON file")


def handle_profiles(args):
    action = getattr(args, "action", None)
    if not action:
        print("Usage: ba2cli.py profiles <action>", file=sys.stderr)
        sys.exit(1)

    if action == "list":
        data = api_call(args, "GET", "/api/jobs/profiles")
        format_output(
            data,
            human=args.human,
            table_fn=lambda d: print_table(
                extract_list(d),
                [
                    ("ID", "id", 6),
                    ("Name", "name", 28),
                    ("Job Type", "job_type", 16),
                    ("Models", "models", 24),
                    ("Created", "created_at", 20),
                ],
            ),
        )

    elif action == "get":
        data = api_call(args, "GET", "/api/jobs/profiles/{}".format(args.id))
        format_output(data, human=args.human)

    elif action == "create":
        body = {"name": args.name}
        if args.json_body is not None:
            body.update(parse_json_arg(args.json_body))
            body["name"] = args.name  # ensure name is not overridden
        if args.description is not None:
            body["description"] = args.description
        data = api_call(args, "POST", "/api/jobs/profiles", data=body)
        format_output(data, human=args.human)

    elif action == "update":
        body = {}
        if args.json_body is not None:
            body.update(parse_json_arg(args.json_body))
        if args.name is not None:
            body["name"] = args.name
        if args.description is not None:
            body["description"] = args.description
        data = api_call(args, "PUT", "/api/jobs/profiles/{}".format(args.id), data=body)
        format_output(data, human=args.human)

    elif action == "delete":
        data = api_call(args, "DELETE", "/api/jobs/profiles/{}".format(args.id))
        format_output(data, human=args.human)

    elif action == "apply":
        body = {"datasetId": args.dataset_id}
        data = api_call(
            args, "POST", "/api/jobs/profiles/{}/apply".format(args.id), data=body
        )
        format_output(data, human=args.human)

    elif action == "export":
        data = api_call(args, "GET", "/api/jobs/profiles/{}/export".format(args.id))
        format_output(data, human=args.human)

    elif action == "import":
        body = parse_json_arg("@" + args.file)
        data = api_call(args, "POST", "/api/jobs/profiles/import", data=body)
        format_output(data, human=args.human)


# ===================================================================
# Resource: models
# ===================================================================

def register_models_commands(subparsers):
    ms = subparsers.add_parser("models", help="Manage trained models")
    actions = ms.add_subparsers(dest="action")

    # list
    p = actions.add_parser("list", help="List all models")
    p.add_argument("--model-type", help="Filter by model type")
    p.add_argument("--status", help="Filter by status")

    # get
    p = actions.add_parser("get", help="Get model by ID")
    p.add_argument("id", help="Model ID (e.g. mdl-abc123)")

    # delete
    p = actions.add_parser("delete", help="Delete a model")
    p.add_argument("id", help="Model ID")

    # clone
    p = actions.add_parser("clone", help="Clone a model")
    p.add_argument("id", help="Model ID")

    # export
    p = actions.add_parser("export", help="Export a model")
    p.add_argument("id", help="Model ID")
    p.add_argument("--format", choices=["pytorch", "onnx"], default="pytorch")

    # predict
    p = actions.add_parser("predict", help="Run predictions with a model")
    p.add_argument("id", help="Model ID")
    p.add_argument("--dataset-id", required=True, type=int)

    # predictions
    p = actions.add_parser("predictions", help="Get model predictions")
    p.add_argument("id", help="Model ID")

    # confusion-matrix
    p = actions.add_parser("confusion-matrix", help="Get confusion matrix")
    p.add_argument("id", help="Model ID")

    # fields
    p = actions.add_parser("fields", help="Get prediction fields")
    p.add_argument("id", help="Model ID")


def handle_models(args):
    action = getattr(args, "action", None)
    if not action:
        print("Usage: ba2cli.py models <action>", file=sys.stderr)
        sys.exit(1)

    if action == "list":
        params = []
        if getattr(args, "model_type", None):
            params.append("model_type={}".format(args.model_type))
        if getattr(args, "status", None):
            params.append("status={}".format(args.status))
        path = "/api/models"
        if params:
            path += "?" + "&".join(params)
        data = api_call(args, "GET", path)
        format_output(
            data,
            human=args.human,
            table_fn=lambda d: print_table(
                extract_list(d),
                [
                    ("ID", "id", 6),
                    ("Name", "name", 28),
                    ("Type", "modelType", 14),
                    ("Status", "status", 12),
                    ("Fitness", "fitness", 10),
                    ("Created", "createdAt", 20),
                ],
            ),
        )

    elif action == "get":
        data = api_call(args, "GET", "/api/models/{}".format(args.id))
        format_output(data, human=args.human)

    elif action == "delete":
        data = api_call(args, "DELETE", "/api/models/{}".format(args.id))
        format_output(data, human=args.human)

    elif action == "clone":
        data = api_call(args, "POST", "/api/models/{}/clone".format(args.id))
        format_output(data, human=args.human)

    elif action == "export":
        path = "/api/models/{}/export/{}".format(args.id, args.format)
        data = api_call(args, "POST", path)
        format_output(data, human=args.human)

    elif action == "predict":
        body = {"dataset_id": args.dataset_id}
        data = api_call(
            args, "POST", "/api/models/{}/run-predictions".format(args.id), data=body
        )
        format_output(data, human=args.human)

    elif action == "predictions":
        data = api_call(args, "GET", "/api/models/{}/predictions".format(args.id))
        format_output(data, human=args.human)

    elif action == "confusion-matrix":
        data = api_call(
            args, "GET", "/api/models/{}/confusion-matrix".format(args.id)
        )
        format_output(data, human=args.human)

    elif action == "fields":
        data = api_call(
            args, "GET", "/api/models/{}/prediction-fields".format(args.id)
        )
        format_output(data, human=args.human)


# ===================================================================
# Resource: strategies
# ===================================================================

def register_strategies_commands(subparsers):
    ss = subparsers.add_parser("strategies", help="Manage trading strategies")
    actions = ss.add_subparsers(dest="action")

    # list
    p = actions.add_parser("list", help="List all strategies")
    p.add_argument("--search", help="Search filter")

    # get
    p = actions.add_parser("get", help="Get strategy by ID")
    p.add_argument("id", type=int)

    # create
    p = actions.add_parser("create", help="Create a strategy")
    p.add_argument("--name", required=True)
    p.add_argument("--description")
    p.add_argument("--buy-conditions", help="JSON string or @file")
    p.add_argument("--sell-conditions", help="JSON string or @file")
    p.add_argument("--exit-conditions", help="JSON array string or @file")
    p.add_argument("--conditions-file", help="Path to JSON file with all condition fields")
    p.add_argument("--tp", type=float, help="Initial take-profit percent")
    p.add_argument("--sl", type=float, help="Initial stop-loss percent")
    p.add_argument("--tp-optimize", action="store_true", help="Enable TP optimization")
    p.add_argument("--sl-optimize", action="store_true", help="Enable SL optimization")
    p.add_argument("--tp-min", type=float)
    p.add_argument("--tp-max", type=float)
    p.add_argument("--tp-step", type=float)
    p.add_argument("--sl-min", type=float)
    p.add_argument("--sl-max", type=float)
    p.add_argument("--sl-step", type=float)

    # update
    p = actions.add_parser("update", help="Update a strategy")
    p.add_argument("id", type=int)
    p.add_argument("--name")
    p.add_argument("--description")
    p.add_argument("--buy-conditions", help="JSON string or @file")
    p.add_argument("--sell-conditions", help="JSON string or @file")
    p.add_argument("--exit-conditions", help="JSON array string or @file")
    p.add_argument("--conditions-file", help="Path to JSON file with all condition fields")
    p.add_argument("--tp", type=float, help="Initial take-profit percent")
    p.add_argument("--sl", type=float, help="Initial stop-loss percent")
    p.add_argument("--tp-optimize", action="store_true", help="Enable TP optimization")
    p.add_argument("--sl-optimize", action="store_true", help="Enable SL optimization")
    p.add_argument("--tp-min", type=float)
    p.add_argument("--tp-max", type=float)
    p.add_argument("--tp-step", type=float)
    p.add_argument("--sl-min", type=float)
    p.add_argument("--sl-max", type=float)
    p.add_argument("--sl-step", type=float)

    # delete
    p = actions.add_parser("delete", help="Delete a strategy")
    p.add_argument("id", type=int)

    # compatible
    p = actions.add_parser("compatible", help="List strategies compatible with a model")
    p.add_argument("--model-id", required=True, type=int)

    # fields
    actions.add_parser("fields", help="Show condition field reference")


def _build_strategy_body(args, require_name=False):
    """Build strategy body from args, shared between create and update."""
    body = {}
    if require_name:
        body["name"] = args.name
    conditions_file = getattr(args, "conditions_file", None)
    if conditions_file:
        body.update(parse_json_arg("@" + conditions_file))
    if require_name:
        body["name"] = args.name  # ensure name is not overridden
    name = getattr(args, "name", None)
    if not require_name and name is not None:
        body["name"] = name
    description = getattr(args, "description", None)
    if description is not None:
        body["description"] = description
    buy_conditions = getattr(args, "buy_conditions", None)
    if buy_conditions is not None:
        body["buy_entry_conditions"] = parse_json_arg(buy_conditions)
    sell_conditions = getattr(args, "sell_conditions", None)
    if sell_conditions is not None:
        body["sell_entry_conditions"] = parse_json_arg(sell_conditions)
    exit_conditions = getattr(args, "exit_conditions", None)
    if exit_conditions is not None:
        body["exit_conditions"] = parse_json_arg(exit_conditions)
    if getattr(args, "tp", None) is not None:
        body["initial_tp_percent"] = args.tp
    if getattr(args, "sl", None) is not None:
        body["initial_sl_percent"] = args.sl
    if getattr(args, "tp_optimize", False):
        body["initial_tp_optimize"] = True
    if getattr(args, "sl_optimize", False):
        body["initial_sl_optimize"] = True
    if getattr(args, "tp_min", None) is not None:
        body["tp_min"] = args.tp_min
    if getattr(args, "tp_max", None) is not None:
        body["tp_max"] = args.tp_max
    if getattr(args, "tp_step", None) is not None:
        body["tp_step"] = args.tp_step
    if getattr(args, "sl_min", None) is not None:
        body["sl_min"] = args.sl_min
    if getattr(args, "sl_max", None) is not None:
        body["sl_max"] = args.sl_max
    if getattr(args, "sl_step", None) is not None:
        body["sl_step"] = args.sl_step
    return body


_STRATEGY_FIELDS_REFERENCE = """\
Field Type          Fields                                          Comparisons
model_class         model:class_0, model:class_1                    is_true, is_false
model_probability   model:probability_0, model:probability_1        gt, gte, lt, lte, eq, neq, between
position            position:in_position, position:is_buy,          is_true/is_false (booleans)
                    position:is_sell, position:position_pnl,        gt, gte, lt, lte (numerics)
                    position:bars_in_position, position:buy_count,
                    position:sell_count, position:total_count
trade               trade:bars_since_last_buy,                      gt, gte, lt, lte, eq, neq
                    trade:bars_since_last_sell,
                    trade:days_since_last_buy,
                    trade:days_since_last_sell
time                time:hour (0-23), time:day_of_week (0=Mon)      gt, gte, lt, lte, eq, neq, between
price               price:change_pct                                gt, gte, lt, lte, eq, neq

Optimization: Set optimize_enabled=true with value_min, value_max, value_step on any condition.
Confirmation: Set confirmation_required=N, confirmation_bars=M to require N true signals in M bars.
"""


def handle_strategies(args):
    action = getattr(args, "action", None)
    if not action:
        print("Usage: ba2cli.py strategies <action>", file=sys.stderr)
        sys.exit(1)

    if action == "list":
        path = "/api/strategies"
        search = getattr(args, "search", None)
        if search:
            path += "?search={}".format(urllib.parse.quote(search, safe=""))
        data = api_call(args, "GET", path)
        format_output(
            data,
            human=args.human,
            table_fn=lambda d: print_table(
                extract_list(d),
                [
                    ("ID", "id", 6),
                    ("Name", "name", 28),
                    ("Description", "description", 30),
                    ("TP%", "initialTpPercent", 8),
                    ("SL%", "initialSlPercent", 8),
                ],
            ),
        )

    elif action == "get":
        data = api_call(args, "GET", "/api/strategies/{}".format(args.id))
        format_output(data, human=args.human)

    elif action == "create":
        body = _build_strategy_body(args, require_name=True)
        data = api_call(args, "POST", "/api/strategies", data=body)
        format_output(data, human=args.human)

    elif action == "update":
        body = _build_strategy_body(args, require_name=False)
        data = api_call(args, "PUT", "/api/strategies/{}".format(args.id), data=body)
        format_output(data, human=args.human)

    elif action == "delete":
        data = api_call(args, "DELETE", "/api/strategies/{}".format(args.id))
        format_output(data, human=args.human)

    elif action == "compatible":
        data = api_call(
            args, "GET", "/api/strategies/compatible/{}".format(args.model_id)
        )
        format_output(data, human=args.human)

    elif action == "fields":
        print(_STRATEGY_FIELDS_REFERENCE)


# ===================================================================
# Resource: backtests
# ===================================================================

def register_backtests_commands(subparsers):
    bs = subparsers.add_parser("backtests", help="Manage backtests")
    actions = bs.add_subparsers(dest="action")

    # list
    actions.add_parser("list", help="List all backtests")

    # get
    p = actions.add_parser("get", help="Get backtest by ID")
    p.add_argument("id", type=int)

    # run
    p = actions.add_parser("run", help="Run a new backtest")
    p.add_argument("--name", required=True)
    p.add_argument("--model-id", required=True, help="Model ID (e.g. mdl-abc123)")
    p.add_argument("--prediction-dataset-id", required=True, type=int)
    p.add_argument("--execution-dataset-id", required=True, type=int)
    p.add_argument("--strategy-id", type=int, help="Strategy ID")
    p.add_argument("--strategy-file", help="Path to JSON file with strategy params")
    p.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    p.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    p.add_argument("--initial-capital", type=float, default=10000)
    p.add_argument("--position-sizing", choices=["fixed", "percent"], default="fixed")
    p.add_argument("--position-value", type=float, default=1000)
    p.add_argument("--commission", type=float, default=0.1)
    p.add_argument("--slippage", type=float, default=0.05)
    p.add_argument("--fitness-metric", help="Fitness metric name")

    # delete
    p = actions.add_parser("delete", help="Delete a backtest")
    p.add_argument("id", type=int)

    # save
    p = actions.add_parser("save", help="Save a backtest")
    p.add_argument("id", type=int)
    p.add_argument("--name", required=True)

    # export
    p = actions.add_parser("export", help="Export backtest results")
    p.add_argument("id", type=int)
    p.add_argument("--format", choices=["csv", "json"], default="csv")

    # compare
    p = actions.add_parser("compare", help="Compare backtests")
    p.add_argument("--ids", required=True, help="Comma-separated backtest IDs")


def handle_backtests(args):
    action = getattr(args, "action", None)
    if not action:
        print("Usage: ba2cli.py backtests <action>", file=sys.stderr)
        sys.exit(1)

    if action == "list":
        data = api_call(args, "GET", "/api/backtests")
        format_output(
            data,
            human=args.human,
            table_fn=lambda d: print_table(
                extract_list(d),
                [
                    ("ID", "id", 6),
                    ("Name", "name", 24),
                    ("Status", "status", 12),
                    ("Return%", "totalReturn", 10),
                    ("Sharpe", "sharpeRatio", 8),
                    ("MaxDD%", "maxDrawdown", 10),
                    ("Trades", "totalTrades", 8),
                ],
            ),
        )

    elif action == "get":
        data = api_call(args, "GET", "/api/backtests/{}".format(args.id))
        format_output(data, human=args.human)

    elif action == "run":
        body = {
            "name": args.name,
            "model_id": args.model_id,
            "prediction_dataset_id": args.prediction_dataset_id,
            "execution_dataset_id": args.execution_dataset_id,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "initial_capital": args.initial_capital,
            "position_sizing_type": args.position_sizing,
            "position_sizing_value": args.position_value,
            "commission": args.commission,
            "slippage": args.slippage,
        }
        if args.strategy_id:
            body["strategy_id"] = args.strategy_id
        if args.strategy_file:
            body["strategy_params"] = parse_json_arg("@" + args.strategy_file)
        if args.fitness_metric:
            body["fitness_metric"] = args.fitness_metric
        data = api_call(args, "POST", "/api/backtests", data=body)
        format_output(data, human=args.human)

    elif action == "delete":
        data = api_call(args, "DELETE", "/api/backtests/{}".format(args.id))
        format_output(data, human=args.human)

    elif action == "save":
        body = {"name": args.name}
        data = api_call(
            args, "POST", "/api/backtests/{}/save".format(args.id), data=body
        )
        format_output(data, human=args.human)

    elif action == "export":
        path = "/api/backtests/{}/export?format={}".format(args.id, args.format)
        data = api_call(args, "POST", path)
        format_output(data, human=args.human)

    elif action == "compare":
        ids = [int(x.strip()) for x in args.ids.split(",")]
        data = api_call(args, "POST", "/api/backtests/compare", data=ids)
        format_output(data, human=args.human)


# ===================================================================
# Resource: cache
# ===================================================================

def register_cache_commands(subparsers):
    cs = subparsers.add_parser("cache", help="Cache and data provider tools")
    actions = cs.add_subparsers(dest="action")

    actions.add_parser("ohlcv-status", help="OHLCV cache status")
    actions.add_parser("ohlcv-gaps", help="Check OHLCV cache gaps")
    actions.add_parser("ohlcv-providers", help="List OHLCV providers")
    actions.add_parser("news-status", help="News cache status")
    actions.add_parser("news-providers", help="List news providers")


def handle_cache(args):
    action = getattr(args, "action", None)
    if not action:
        print("Usage: ba2cli.py cache <action>", file=sys.stderr)
        sys.exit(1)

    if action == "ohlcv-status":
        data = api_call(args, "GET", "/api/tools/ohlcv/cache-status")
        format_output(
            data,
            human=args.human,
            table_fn=lambda d: print_table(
                extract_list(d),
                [
                    ("Provider", "provider", 12),
                    ("Symbol", "symbol", 10),
                    ("Interval", "interval", 10),
                    ("Rows", "rows", 8),
                    ("Date From", "date_from", 12),
                    ("Date To", "date_to", 12),
                    ("Size MB", "file_size_mb", 10),
                ],
            ),
        )

    elif action == "ohlcv-gaps":
        data = api_call(args, "GET", "/api/tools/ohlcv/check-gaps")
        format_output(data, human=args.human)

    elif action == "ohlcv-providers":
        data = api_call(args, "GET", "/api/tools/ohlcv/providers")
        format_output(data, human=args.human)

    elif action == "news-status":
        data = api_call(args, "GET", "/api/tools/news/cache-status")
        format_output(data, human=args.human)

    elif action == "news-providers":
        data = api_call(args, "GET", "/api/tools/news/providers")
        format_output(data, human=args.human)


# ===================================================================
# Resource: workers
# ===================================================================

def register_workers_commands(subparsers):
    ws = subparsers.add_parser("workers", help="Manage workers")
    actions = ws.add_subparsers(dest="action")

    # list
    actions.add_parser("list", help="List all workers")

    # get
    p = actions.add_parser("get", help="Get worker by ID")
    p.add_argument("id")

    # enable
    p = actions.add_parser("enable", help="Enable a worker")
    p.add_argument("id")

    # disable
    p = actions.add_parser("disable", help="Disable a worker")
    p.add_argument("id")

    # status
    p = actions.add_parser("status", help="Get worker status")
    p.add_argument("id")


def handle_workers(args):
    action = getattr(args, "action", None)
    if not action:
        print("Usage: ba2cli.py workers <action>", file=sys.stderr)
        sys.exit(1)

    if action == "list":
        data = api_call(args, "GET", "/api/workers")
        format_output(data, human=args.human)

    elif action == "get":
        data = api_call(args, "GET", "/api/workers/{}".format(args.id))
        format_output(data, human=args.human)

    elif action == "enable":
        data = api_call(args, "POST", "/api/workers/{}/enable".format(args.id))
        format_output(data, human=args.human)

    elif action == "disable":
        data = api_call(args, "POST", "/api/workers/{}/disable".format(args.id))
        format_output(data, human=args.human)

    elif action == "status":
        data = api_call(args, "GET", "/api/workers/{}/status".format(args.id))
        format_output(data, human=args.human)


# ===================================================================
# Resource: tasks
# ===================================================================

def register_tasks_commands(subparsers):
    ts = subparsers.add_parser("tasks", help="Manage tasks")
    actions = ts.add_subparsers(dest="action")

    # list
    actions.add_parser("list", help="List all tasks")

    # get
    p = actions.add_parser("get", help="Get task by ID")
    p.add_argument("id")

    # cancel
    p = actions.add_parser("cancel", help="Cancel a task")
    p.add_argument("id")

    # stats
    actions.add_parser("stats", help="Get task statistics summary")


def handle_tasks(args):
    action = getattr(args, "action", None)
    if not action:
        print("Usage: ba2cli.py tasks <action>", file=sys.stderr)
        sys.exit(1)

    if action == "list":
        data = api_call(args, "GET", "/api/tasks")
        format_output(data, human=args.human)

    elif action == "get":
        data = api_call(args, "GET", "/api/tasks/{}".format(args.id))
        format_output(data, human=args.human)

    elif action == "cancel":
        data = api_call(args, "POST", "/api/tasks/{}/cancel".format(args.id))
        format_output(data, human=args.human)

    elif action == "stats":
        data = api_call(args, "GET", "/api/tasks/stats/summary")
        format_output(data, human=args.human)


# ===================================================================
# Resource: settings
# ===================================================================

def register_settings_commands(subparsers):
    ss = subparsers.add_parser("settings", help="Manage settings")
    actions = ss.add_subparsers(dest="action")

    # get
    actions.add_parser("get", help="Get current settings")

    # update
    p = actions.add_parser("update", help="Update settings")
    p.add_argument("--json", dest="json_body", required=True,
                   help="JSON string or @file")

    # gpu-info
    actions.add_parser("gpu-info", help="Get GPU information")

    # system-info
    actions.add_parser("system-info", help="Get system information")


def handle_settings(args):
    action = getattr(args, "action", None)
    if not action:
        print("Usage: ba2cli.py settings <action>", file=sys.stderr)
        sys.exit(1)

    if action == "get":
        data = api_call(args, "GET", "/api/settings")
        format_output(data, human=args.human)

    elif action == "update":
        body = parse_json_arg(args.json_body)
        data = api_call(args, "PUT", "/api/settings", data=body)
        format_output(data, human=args.human)

    elif action == "gpu-info":
        data = api_call(args, "GET", "/api/settings/gpu-info")
        format_output(data, human=args.human)

    elif action == "system-info":
        data = api_call(args, "GET", "/api/settings/system-info")
        format_output(data, human=args.human)


# ===================================================================
# Resource: server
# ===================================================================

def register_server_commands(subparsers):
    sv = subparsers.add_parser("server", help="Server administration")
    actions = sv.add_subparsers(dest="action")

    # health
    actions.add_parser("health", help="Check server health")

    # update
    actions.add_parser("update", help="Trigger server update")

    # db-cleanup
    actions.add_parser("db-cleanup", help="Clean up DB: clear stale results, VACUUM")


def handle_server(args):
    action = getattr(args, "action", None)
    if not action:
        print("Usage: ba2cli.py server <action>", file=sys.stderr)
        sys.exit(1)

    if action == "health":
        data = api_call(args, "GET", "/health")
        format_output(data, human=args.human)

    elif action == "update":
        data = api_call(args, "POST", "/api/admin/update")
        format_output(data, human=args.human)

    elif action == "db-cleanup":
        data = api_call(args, "POST", "/api/admin/db-cleanup")
        format_output(data, human=args.human)


# ===================================================================
# Resource: ml
# ===================================================================

def register_ml_commands(subparsers):
    ml = subparsers.add_parser("ml", help="ML engine information")
    actions = ml.add_subparsers(dest="action")

    actions.add_parser("models", help="List available ML model types")
    actions.add_parser("classification-models", help="List classification model types")
    actions.add_parser("system-info", help="Get ML system info")
    actions.add_parser("gpu-status", help="Get GPU status")


def handle_ml(args):
    action = getattr(args, "action", None)
    if not action:
        print("Usage: ba2cli.py ml <action>", file=sys.stderr)
        sys.exit(1)

    if action == "models":
        data = api_call(args, "GET", "/api/ml/models")
        format_output(data, human=args.human)

    elif action == "classification-models":
        data = api_call(args, "GET", "/api/ml/classification-models")
        format_output(data, human=args.human)

    elif action == "system-info":
        data = api_call(args, "GET", "/api/ml/system-info")
        format_output(data, human=args.human)

    elif action == "gpu-status":
        data = api_call(args, "GET", "/api/ml/gpu-status")
        format_output(data, human=args.human)


# ===================================================================
# Resource: dashboard
# ===================================================================

def register_dashboard_commands(subparsers):
    db = subparsers.add_parser("dashboard", help="Dashboard data")
    actions = db.add_subparsers(dest="action")

    actions.add_parser("stats", help="Get dashboard statistics")


def handle_dashboard(args):
    action = getattr(args, "action", None)
    if not action:
        print("Usage: ba2cli.py dashboard <action>", file=sys.stderr)
        sys.exit(1)

    if action == "stats":
        data = api_call(args, "GET", "/api/dashboard/stats")
        format_output(data, human=args.human)


# ===================================================================
# Resource: logs
# ===================================================================

def register_logs_commands(subparsers):
    lg = subparsers.add_parser("logs", help="Read server log files")
    actions = lg.add_subparsers(dest="action")

    # info
    p = actions.add_parser("info", help="Read info log")
    p.add_argument("--lines", type=int, default=100, help="Number of lines (default: 100)")
    p.add_argument("--search", help="Filter lines containing this string")

    # error
    p = actions.add_parser("error", help="Read error log")
    p.add_argument("--lines", type=int, default=100, help="Number of lines (default: 100)")
    p.add_argument("--search", help="Filter lines containing this string")

    # debug
    p = actions.add_parser("debug", help="Read debug log")
    p.add_argument("--lines", type=int, default=100, help="Number of lines (default: 100)")
    p.add_argument("--search", help="Filter lines containing this string")


def handle_logs(args):
    action = getattr(args, "action", None)
    if not action:
        print("Usage: ba2cli.py logs <action>", file=sys.stderr)
        sys.exit(1)

    if action in ("info", "error", "debug"):
        path = "/api/admin/logs/{}?lines={}".format(action, args.lines)
        if args.search:
            path += "&search={}".format(urllib.parse.quote(args.search, safe=""))
        data = api_call(args, "GET", path)
        if args.human and data and isinstance(data, dict) and "lines" in data:
            for line in data["lines"]:
                print(line)
        else:
            format_output(data, human=args.human)


# ===================================================================
# Resource: help
# ===================================================================

_MANUAL = {}

_MANUAL["overview"] = """\
BA2 ML Test Platform CLI
========================

ba2cli.py is the command-line interface for the BA2 ML Test Platform.  It wraps
the FastAPI backend into a scriptable tool designed for both human operators and
autonomous LLM agents.

Every command follows the pattern:

    ba2cli.py [global-options] <resource> <action> [arguments]

Output is JSON by default (ideal for programmatic consumption).  Pass --human
for aligned text tables where supported.
"""

_MANUAL["global_options"] = """\
Global Options
--------------

  --host HOST    API host (default: 127.0.0.1)
  --port PORT    API port (default: 8000)
  --human        Human-readable table output instead of JSON
  --token TOKEN  Auth token (default: $BA2_ADMIN_TOKEN env var)
"""

_MANUAL["quick_reference"] = """\
Quick Reference
---------------

Resource      Actions
----------    ---------------------------------------------------------------
datasets      list | get | create | delete | rename | duplicate | regenerate
              | preview | stats | columns | export
targets       list | get | create | update | delete | preview
indicators    list | get | create | update | delete | supported
jobs          list | get | create | prepare | delete | progress | pause
              | resume | cancel | logs | generations | individuals
              | elite-models | save-model
profiles      list | get | create | update | delete | apply | export | import
models        list | get | delete | clone | export | predict | predictions
              | confusion-matrix | fields
strategies    list | get | create | update | delete | compatible | fields
backtests     list | get | run | delete | save | export | compare
cache         ohlcv-status | ohlcv-gaps | ohlcv-providers | news-status
              | news-providers
workers       list | get | enable | disable | status
tasks         list | get | cancel | stats
settings      get | update | gpu-info | system-info
server        health | update
ml            models | classification-models | system-info | gpu-status
dashboard     stats
logs          info | error | debug
help          [topic]
"""

_MANUAL["json_input"] = """\
JSON Input
----------

Many commands accept JSON arguments (targets, param-ranges, genetic-config,
etc.).  You can provide them in two ways:

  Inline JSON:
    ba2cli.py targets create --name "t1" --targets '[{"type":"binary_up","threshold":0.5}]'

  File reference (prefix with @):
    ba2cli.py targets create --name "t1" --targets @targets.json

The @file syntax works for any argument documented as "JSON string or @file".
"""

_MANUAL["workflow"] = """\
Agentic Workflow
----------------

A typical autonomous optimization loop follows these steps:

  1. Check cached data
       ba2cli.py cache ohlcv-status

  2. Create a dataset
       ba2cli.py datasets create --ticker AAPL --timeframe 1d \\
           --indicator-collection-id 1

  3. Preview targets on the dataset
       ba2cli.py targets preview --dataset-id 1 \\
           --targets '[{"type":"binary_up","threshold":0.5,"horizon":3}]'

  4. Prepare a job (calculate targets, get metric recommendations)
       ba2cli.py jobs prepare --dataset-id 1 \\
           --targets '[{"type":"binary_up","threshold":0.5,"horizon":3}]'

  5. Create a training job
       ba2cli.py jobs create --dataset-id 1 \\
           --model-types LSTM,GRU \\
           --targets '[{"type":"binary_up","threshold":0.5,"horizon":3}]' \\
           --train-test-split 80 \\
           --param-ranges '{"epochs":{"min":50,"max":200},"batch_size":{"min":16,"max":64}}' \\
           --genetic-config '{"populationSize":20,"generations":10}' \\
           --metrics-config '{"metric":"f1_score","loss_function":"focal_loss"}'

  6. Poll progress until completion
       ba2cli.py jobs progress <job-id>

  7. Save the best model
       ba2cli.py jobs save-model <job-id> --rank 1 --name "model_v1"

  8. Create a trading strategy
       ba2cli.py strategies create --name "v1" \\
           --buy-conditions '[{"field":"model:class_1","comparison":"is_true"}]' \\
           --sell-conditions '[{"field":"model:class_0","comparison":"is_true"}]' \\
           --tp 2.0 --sl 1.0

  9. Run a backtest
       ba2cli.py backtests run --name "test1" \\
           --model-id mdl-xxx \\
           --prediction-dataset-id 1 --execution-dataset-id 1 \\
           --start-date 2024-01-01 --end-date 2024-12-31

 10. Check results and compare
       ba2cli.py backtests get <id>
       ba2cli.py backtests compare --ids 1,2,3
"""

_MANUAL["conditions"] = """\
Strategy Conditions Reference
-----------------------------

Field Type          Fields                                          Comparisons
----------------    --------------------------------------------    ---------------------------
model_class         model:class_0, model:class_1                    is_true, is_false
model_probability   model:probability_0, model:probability_1        gt, gte, lt, lte, eq, neq,
                                                                    between
position            position:in_position, position:is_buy,          is_true / is_false (bools)
                    position:is_sell, position:position_pnl,        gt, gte, lt, lte (numerics)
                    position:bars_in_position, position:buy_count,
                    position:sell_count, position:total_count
trade               trade:bars_since_last_buy,                      gt, gte, lt, lte, eq, neq
                    trade:bars_since_last_sell,
                    trade:days_since_last_buy,
                    trade:days_since_last_sell
time                time:hour (0-23), time:day_of_week (0=Mon)      gt, gte, lt, lte, eq, neq,
                                                                    between
price               price:change_pct                                gt, gte, lt, lte, eq, neq

Optimization:   Set optimize_enabled=true with value_min, value_max, value_step
                on any condition to let the genetic optimizer search its value.

Confirmation:   Set confirmation_required=N, confirmation_bars=M to require
                N true signals within the last M bars before the condition fires.

Example condition JSON:
  {
    "field": "model:probability_1",
    "comparison": "gt",
    "value": 0.6,
    "optimize_enabled": true,
    "value_min": 0.5,
    "value_max": 0.9,
    "value_step": 0.05
  }
"""

_MANUAL["jobs_reference"] = """\
Job Configuration Reference
----------------------------

ParameterRanges (--param-ranges):
  {
    "epochs":             {"min": 50,   "max": 200},
    "batch_size":         {"min": 16,   "max": 128},
    "learning_rate":      {"min": 1e-4, "max": 1e-2},
    "hidden_size":        {"min": 32,   "max": 256},
    "num_layers":         {"min": 1,    "max": 4},
    "dropout":            {"min": 0.1,  "max": 0.5},
    "sequence_length":    {"min": 10,   "max": 60}
  }
  Each field is an object with "min" and "max" (integers or floats).
  The genetic optimizer samples uniformly within these ranges.

GeneticConfig (--genetic-config):
  {
    "populationSize":     20,       // individuals per generation
    "generations":        10,       // number of generations
    "crossoverRate":      0.8,      // probability of crossover
    "mutationRate":       0.2,      // probability of mutation
    "eliteCount":         2,        // individuals preserved unchanged
    "tournamentSize":     3         // tournament selection size
  }

MetricsConfig (--metrics-config):
  {
    "metric":         "f1_score",       // fitness metric to optimize
    "loss_function":  "focal_loss",     // loss function for training
    "threshold":      0.5              // classification threshold
  }
  Supported metrics: accuracy, balanced_accuracy, f1_score, precision,
                     recall, roc_auc, mcc
  Supported losses:  cross_entropy, focal_loss, weighted_cross_entropy

CrossValidation (--cross-validation):
  {
    "enabled":  true,
    "folds":    5,
    "method":   "time_series"       // time_series or stratified
  }
"""

_MANUAL["strategies"] = _MANUAL["conditions"]

_MANUAL["backtests"] = """\
Backtests Reference
-------------------

Running a backtest:
  ba2cli.py backtests run --name "test1" \\
      --model-id mdl-xxx \\
      --prediction-dataset-id 1 \\
      --execution-dataset-id 1 \\
      --strategy-id 5 \\
      --start-date 2024-01-01 --end-date 2024-12-31 \\
      --initial-capital 10000 \\
      --position-sizing fixed --position-value 1000 \\
      --commission 0.1 --slippage 0.05

  --model-id              Saved model ID (e.g. mdl-abc123)
  --prediction-dataset-id Dataset used for generating predictions
  --execution-dataset-id  Dataset used for trade execution (can differ in
                          timeframe from prediction dataset)
  --strategy-id           ID of a saved strategy (or use --strategy-file)
  --strategy-file         Path to a JSON file with inline strategy params
  --position-sizing       "fixed" (absolute $) or "percent" (% of equity)
  --fitness-metric        Metric used to rank backtest results

Comparing backtests:
  ba2cli.py backtests compare --ids 1,2,3

  Returns a side-by-side comparison of key metrics (return, Sharpe ratio,
  max drawdown, win rate, total trades, etc.)
"""

_MANUAL["cache"] = """\
Cache Reference
---------------

The cache resource lets you inspect locally cached market data.

  ba2cli.py cache ohlcv-status       Show cached OHLCV files with date ranges
  ba2cli.py cache ohlcv-gaps         Check for gaps in OHLCV cache
  ba2cli.py cache ohlcv-providers    List available OHLCV data providers
  ba2cli.py cache news-status        Show cached news data
  ba2cli.py cache news-providers     List available news providers

Before creating a dataset, check the cache to see if the required data is
already available.  This avoids unnecessary downloads.
"""

_TOPIC_ALIASES = {
    "strategy": "strategies",
    "condition": "conditions",
    "fields": "conditions",
    "job": "jobs_reference",
    "jobs": "jobs_reference",
    "backtest": "backtests",
    "workflow": "workflow",
    "cache": "cache",
    "strategies": "strategies",
    "conditions": "conditions",
    "backtests": "backtests",
    "json": "json_input",
    "options": "global_options",
    "reference": "quick_reference",
}


def register_help_commands(subparsers):
    hp = subparsers.add_parser("help", help="Show manual and help topics")
    hp.add_argument(
        "topic",
        nargs="?",
        default=None,
        help="Topic: strategies, jobs, backtests, workflow, conditions, cache",
    )


def handle_help(args):
    topic = getattr(args, "topic", None)
    if topic is None:
        # Print the full manual
        sections = [
            "overview",
            "global_options",
            "quick_reference",
            "json_input",
            "workflow",
            "conditions",
            "jobs_reference",
        ]
        for section in sections:
            print(_MANUAL[section])
    else:
        key = _TOPIC_ALIASES.get(topic)
        if key is None:
            print("Unknown help topic: {}".format(topic), file=sys.stderr)
            print("", file=sys.stderr)
            print(
                "Available topics: strategies, jobs, backtests, workflow, "
                "conditions, cache, json, options, reference",
                file=sys.stderr,
            )
            sys.exit(1)
        print(_MANUAL[key])


# ===================================================================
# Main / arg-parse wiring
# ===================================================================

# Map resource name -> (register_fn, handle_fn)
def main():
    parser = argparse.ArgumentParser(
        prog="ba2cli",
        description="BA2 ML Test Platform CLI",
    )
    parser.add_argument("--host", default="127.0.0.1", help="API host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="API port (default: 8000)")
    parser.add_argument("--human", action="store_true", help="Human-readable table output")
    parser.add_argument(
        "--token",
        default=os.environ.get("BA2_ADMIN_TOKEN"),
        help="Auth token (default: $BA2_ADMIN_TOKEN)",
    )

    resource_parsers = parser.add_subparsers(dest="resource")

    # Register all resources
    register_datasets_commands(resource_parsers)
    register_targets_commands(resource_parsers)
    register_indicators_commands(resource_parsers)
    register_jobs_commands(resource_parsers)
    register_profiles_commands(resource_parsers)
    register_models_commands(resource_parsers)
    register_strategies_commands(resource_parsers)
    register_backtests_commands(resource_parsers)
    register_cache_commands(resource_parsers)
    register_workers_commands(resource_parsers)
    register_tasks_commands(resource_parsers)
    register_settings_commands(resource_parsers)
    register_server_commands(resource_parsers)
    register_ml_commands(resource_parsers)
    register_dashboard_commands(resource_parsers)
    register_logs_commands(resource_parsers)
    register_help_commands(resource_parsers)

    # Allow global flags (--human, --host, --port, --token) to appear anywhere
    # in the command line, not just before the resource subcommand.
    argv = sys.argv[1:]
    global_flags = {}
    if "--human" in argv:
        argv.remove("--human")
        global_flags["human"] = True

    args = parser.parse_args(argv)

    # Merge pre-extracted global flags
    for key, val in global_flags.items():
        setattr(args, key, val)

    if not args.resource:
        parser.print_help()
        sys.exit(0)

    # Dispatch
    handlers = {
        "datasets": handle_datasets,
        "targets": handle_targets,
        "indicators": handle_indicators,
        "jobs": handle_jobs,
        "profiles": handle_profiles,
        "models": handle_models,
        "strategies": handle_strategies,
        "backtests": handle_backtests,
        "cache": handle_cache,
        "workers": handle_workers,
        "tasks": handle_tasks,
        "settings": handle_settings,
        "server": handle_server,
        "ml": handle_ml,
        "dashboard": handle_dashboard,
        "logs": handle_logs,
        "help": handle_help,
    }

    handler = handlers.get(args.resource)
    if handler:
        handler(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
