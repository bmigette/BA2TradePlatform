"""Batch-import upload regression: the handler must read NiceGUI 3's `e.file`.

`_show_batch_import_dialog`'s upload handler read `e.content.read()` — the NiceGUI 2.x
event shape. On NiceGUI 3.x `UploadEventArguments` has only `file` (a `FileUpload` whose
`read()` is ASYNC), so every upload attempt raised AttributeError, which the handler's own
except rendered as a misleading "Could not read that file". The fix mirrors the other three
upload handlers in settings.py (`await e.file.read()`).

The test drives the REAL dialog builder and the REAL upload element:
`Upload.handle_uploads` is the seam NiceGUI itself documents "for simulating file uploads
in tests", and the payload is a real `SmallFileUpload`. No fake event namespace — faking
`e.content` is exactly what let this ship. NiceGUI dispatches async handlers through
`background_tasks.create_or_defer`, which defers to app startup when `core.loop` is not
running, so the test sets it to its own loop and awaits the spawned tasks itself.
"""
import asyncio
import json
from types import SimpleNamespace as NS

from nicegui import background_tasks, core, ui
from nicegui.elements.upload_files import SmallFileUpload

from ba2_trade_platform.core.expert_batch_export_import import EXPORT_TYPE
from ba2_trade_platform.ui.pages import settings as settings_module


def _valid_payload_bytes() -> bytes:
    return json.dumps({
        "export_version": "1.0",
        "export_type": EXPORT_TYPE,
        "experts": [],
    }).encode("utf-8")


def _build_dialog_with_real_upload():
    """Run the real `_show_batch_import_dialog` on a fake self; capture the Upload it creates.

    `__init__` of the tab is not needed: the method only touches `self._render_import_plan`
    (on success), recorded by the fake. Element construction works headlessly (script mode).
    """
    rendered = []
    fake_self = NS(_render_import_plan=lambda preview, plan, dialog: rendered.append(plan))
    existing = set(ui.context.client.elements.values())
    settings_module.ExpertSettingsTab._show_batch_import_dialog(fake_self)
    uploads = [e for e in ui.context.client.elements.values()
               if isinstance(e, ui.upload) and e not in existing]
    assert uploads, "the dialog did not create a ui.upload"
    return uploads[0], rendered


async def _dispatch(upload, file):
    """Send one upload event through the REAL element, letting nicegui's own dispatch run.

    `handle_uploads` -> `handle_event` schedules the async handler via
    `background_tasks.create_or_defer`, which needs `core.loop` to be OUR running loop
    (otherwise it defers to app startup and never runs here).
    """
    prev_loop = core.loop
    core.loop = asyncio.get_running_loop()
    known = set(background_tasks.running_tasks)
    try:
        await upload.handle_uploads([file])
        spawned = set(background_tasks.running_tasks) - known
        await asyncio.gather(*spawned)
    finally:
        core.loop = prev_loop


def test_a_valid_batch_file_reaches_the_plan_renderer():
    """The handler parses and plans a real batch export — no 'could not read' error."""
    upload, rendered = _build_dialog_with_real_upload()
    file = SmallFileUpload(name="expert_batch_1.json",
                           content_type="application/json",
                           _data=_valid_payload_bytes())

    asyncio.run(_dispatch(upload, file))

    assert len(rendered) == 1, (
        "the valid payload never reached _render_import_plan — the handler still fails to "
        "read the upload event (e.content vs e.file / sync vs async read)")


def test_a_garbage_file_reports_a_parse_error_not_an_attribute_error():
    """A genuinely broken file must fail LOUDLY as a parse problem, not as an AttributeError.

    Before the fix, even a VALID file produced "has no attribute 'content'"; after it, the
    error path still exists for real bad files — that distinction is what the dialog shows
    the operator.
    """
    upload, rendered = _build_dialog_with_real_upload()
    file = SmallFileUpload(name="notes.txt",
                           content_type="text/plain",
                           _data=b"this is not json at all")

    asyncio.run(_dispatch(upload, file))

    assert rendered == [], "a garbage file must never reach the plan renderer"
    labels = [getattr(e, 'text', '') for e in ui.context.client.elements.values()
              if isinstance(e, ui.label)]
    errors = [t for t in labels if t.startswith('Could not read that file:')]
    assert errors, f"no error label rendered; labels seen: {labels!r}"
    assert "attribute" not in errors[-1].lower(), (
        f"the error is still an AttributeError leaking through: {errors[-1]!r}")
