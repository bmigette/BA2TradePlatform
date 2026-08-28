"""
Genetic Optimization Service

Implements genetic algorithm optimization for model hyperparameters
using DEAP (Distributed Evolutionary Algorithms in Python).
"""

import numpy as np
from datetime import datetime
from typing import Dict, List, Any, Optional, Callable, Tuple
import logging
import os
import random
import json

logger = logging.getLogger(__name__)

# Check for DEAP availability
try:
    from deap import base, creator, tools, algorithms
    DEAP_AVAILABLE = True
    logger.info("DEAP library available")
except ImportError:
    DEAP_AVAILABLE = False
    logger.warning("DEAP not available. Install with: pip install deap")


def _np_state_to_jsonable(state):
    """Convert a numpy random state tuple to a JSON-serializable list.

    numpy's get_state() returns (name, ndarray[uint32], pos, has_gauss, cached);
    the ndarray must be turned into a plain list for JSON checkpoint storage.
    """
    name, keys, pos, has_gauss, cached = state
    return [name, keys.tolist(), int(pos), int(has_gauss), float(cached)]


def _jsonable_to_np_state(s):
    """Inverse of _np_state_to_jsonable: rebuild a numpy random state tuple."""
    name, keys, pos, has_gauss, cached = s
    return (name, np.array(keys, dtype=np.uint32), int(pos), int(has_gauss), float(cached))


def _py_state_to_jsonable(state):
    """Convert the stdlib ``random`` state tuple to a JSON-serializable list.

    random.getstate() returns (version, internal_state, gauss_next) where internal_state is a
    625-int TUPLE; a plain list() of the outer tuple leaves it nested, and the JSON checkpoint
    column then turns it into a list that setstate() refuses. Flatten it explicitly here so
    _jsonable_to_py_state can rebuild it explicitly (mirrors the numpy pair above).
    """
    version, internal_state, gauss_next = state
    return [
        int(version),
        [int(x) for x in internal_state],
        None if gauss_next is None else float(gauss_next),
    ]


def _jsonable_to_py_state(s):
    """Inverse of _py_state_to_jsonable: rebuild a stdlib random state tuple.

    Both elements must be tuples again -- setstate() raises "state vector must be a tuple" on
    the list JSON gives back. LEGACY checkpoints (written as list(random.getstate()) before this
    pair existed) have the identical post-JSON shape, so they rebuild through here unchanged.
    """
    version, internal_state, gauss_next = s
    return (
        int(version),
        tuple(int(x) for x in internal_state),
        None if gauss_next is None else float(gauss_next),
    )


def _clone_global_rng() -> random.Random:
    """A private ``random.Random`` carrying the CURRENT global ``random`` module state.

    This is how the GA inherits its caller's seed without an API change and without consuming a
    single draw: ``handle_strategy_optimization`` does ``random.seed(ga["seed"])`` and then
    constructs the optimizer, so a snapshot taken at construction time IS the seeded stream.
    Because ``random.Random`` and the module-level functions are the same Mersenne Twister with
    the same methods, the private generator then yields byte-identical numbers to the ones the
    module would have produced -- so switching the GA onto it changes nothing except WHO can
    disturb it.

    Which is the entire point: see ``GeneticOptimizer._rng``.
    """
    rng = random.Random()
    rng.setstate(random.getstate())
    return rng


def _accepts_on_result(batch_fitness) -> bool:
    """Does this batch evaluator take the incremental-result callback?

    Older/simpler evaluators (brute force, the ML GA) take only the param-dict list. An
    unintrospectable callable is treated as NOT accepting it: falling back costs a
    mid-generation checkpoint, whereas guessing wrong raises a TypeError that would be
    indistinguishable from a failure inside the evaluation itself.
    """
    import inspect

    try:
        params = inspect.signature(batch_fitness).parameters
    except (TypeError, ValueError):
        return False
    if "on_result" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class GeneticOptimizer:
    """
    Genetic algorithm optimizer for model hyperparameters.

    Uses DEAP to evolve populations of hyperparameter configurations
    to find optimal model architectures.
    """

    # Maximum number of layers for per-layer optimization
    MAX_LAYERS = 4

    # Default hyperparameter ranges
    # Per-layer sizes: hidden_dim_layer_1, hidden_dim_layer_2, etc.
    # These are combined into a list based on n_rnn_layers during decode
    DEFAULT_PARAM_RANGES = {
        'hidden_dim_layer_1': {'min': 16, 'max': 256, 'step': 16, 'type': 'int'},
        'hidden_dim_layer_2': {'min': 16, 'max': 256, 'step': 16, 'type': 'int'},
        'hidden_dim_layer_3': {'min': 16, 'max': 256, 'step': 16, 'type': 'int'},
        'hidden_dim_layer_4': {'min': 16, 'max': 256, 'step': 16, 'type': 'int'},
        'n_rnn_layers': {'min': 1, 'max': 4, 'step': 1, 'type': 'int'},
        'dropout': {'min': 0.0, 'max': 0.5, 'step': 0.1, 'type': 'float'},
        'learning_rate': {'min': 0.0001, 'max': 0.01, 'step': 0.0001, 'type': 'float'},
        'batch_size': {'min': 16, 'max': 128, 'step': 16, 'type': 'int'},
        'input_chunk_length': {'min': 10, 'max': 60, 'step': 5, 'type': 'int'}
    }

    def __init__(
        self,
        param_ranges: Dict = None,
        population_size: int = 20,
        n_generations: int = 10,
        crossover_prob: float = 0.7,
        mutation_prob: float = 0.2,
        early_stopping_generations: int = 3,
        elitism_percent: float = 10.0,
        parallel_individuals: int = 1
    ):
        """
        Initialize GeneticOptimizer.

        Args:
            param_ranges: Dictionary of parameter ranges to optimize
            population_size: Number of individuals in population
            n_generations: Number of generations to evolve
            crossover_prob: Probability of crossover
            mutation_prob: Probability of mutation
            early_stopping_generations: Stop if no improvement for this many generations
            elitism_percent: Percentage of best individuals to preserve unchanged (default 10%)
        """
        if not DEAP_AVAILABLE:
            raise RuntimeError("DEAP library not available. Install with: pip install deap")

        self.param_ranges = param_ranges or self.DEFAULT_PARAM_RANGES
        self.population_size = population_size
        self.n_generations = n_generations
        self.crossover_prob = crossover_prob
        self.mutation_prob = mutation_prob
        self.early_stopping_generations = early_stopping_generations
        self.elitism_percent = elitism_percent
        self.parallel_individuals = max(1, parallel_individuals)

        # THE GA'S OWN RANDOMNESS, isolated from the fitness evaluation's.
        #
        # Every draw the search itself makes -- the initial population, tournament selection, the
        # crossover/mutation gates, the crossover points, the mutation gaussians -- comes from
        # here and NEVER from the global ``random`` module. Seeded by cloning the module state at
        # construction (the caller has just done ``random.seed(seed)``), so the sequence is
        # exactly the one the module would have produced.
        #
        # WHY (bug F4: a GA result depended on ``--parallel``). The fitness evaluation runs in
        # this same process when ``parallel <= 1`` and no remote workers are configured
        # (``strategy_optimization_handler._dispatch_engages`` -> ``batch_fitness is None`` ->
        # the ``map(self.toolbox.evaluate, ...)`` branch below), and in spawned worker PROCESSES
        # otherwise. ``DailyBacktestEngine.run()`` opens with ``random.seed(self.seed)`` for its
        # own reproducibility -- harmless in a worker, but in-process it RESET the GA's stream
        # after every single trial. Measured on 5 symbols, pop=16 gen=3, seed=42: parallel=4 and
        # 8 gave -6.04 while parallel=1 gave -43.66, with generation 0 (drawn before any
        # evaluation) identical in both and every later generation different. A private
        # generator makes the search depend on (seed, population, generations) alone; nothing a
        # fitness function does to the global RNG can reach it.
        self._rng = _clone_global_rng()

        self.toolbox = None
        self.best_individual = None
        self.best_fitness = None
        self.history = []

        # Write a partial checkpoint every N completed trials within a generation. Bounds what a
        # restart loses to N trials instead of a whole generation. 0 disables.
        try:
            self.partial_checkpoint_every = int(os.getenv("BT_PARTIAL_CHECKPOINT_EVERY", "10"))
        except ValueError:
            logger.warning(
                "BT_PARTIAL_CHECKPOINT_EVERY="
                f"{os.getenv('BT_PARTIAL_CHECKPOINT_EVERY')!r} is not an integer; using 10")
            self.partial_checkpoint_every = 10
        self._partial_counter = 0

        self._setup_deap()

    def _setup_deap(self):
        """Set up DEAP toolbox with genetic operators."""
        # Create fitness and individual classes
        if not hasattr(creator, 'FitnessMax'):
            creator.create("FitnessMax", base.Fitness, weights=(1.0,))
        if not hasattr(creator, 'Individual'):
            creator.create("Individual", list, fitness=creator.FitnessMax)

        self.toolbox = base.Toolbox()

        # Register attribute generators for each parameter
        self.param_names = list(self.param_ranges.keys())
        for i, (param_name, config) in enumerate(self.param_ranges.items()):
            if config['type'] == 'int':
                self.toolbox.register(
                    f"attr_{i}",
                    self._rng.randint,
                    config['min'],
                    config['max']
                )
            else:
                self.toolbox.register(
                    f"attr_{i}",
                    self._rng.uniform,
                    config['min'],
                    config['max']
                )

        # Register individual and population creators
        n_params = len(self.param_ranges)
        self.toolbox.register(
            "individual",
            self._create_individual
        )
        self.toolbox.register(
            "population",
            tools.initRepeat,
            list,
            self.toolbox.individual
        )

        # Register genetic operators.
        #
        # ``mate`` and ``select`` are LOCAL re-implementations of ``tools.cxTwoPoint`` and
        # ``tools.selTournament`` rather than the DEAP originals: those draw from the global
        # ``random`` module (they are documented as doing so) and there is no seam to hand them
        # a generator. Routing them through ``self._rng`` is what keeps the whole search immune
        # to a fitness function touching the global RNG -- see ``self._rng``. The draw sequence
        # is identical to DEAP's, so this changes no result on its own.
        self.toolbox.register("mate", self._cx_two_point)
        self.toolbox.register("mutate", self._mutate_individual)
        self.toolbox.register("select", self._sel_tournament, tournsize=3)

    def _cx_two_point(self, ind1: List, ind2: List) -> Tuple[List, List]:
        """``tools.cxTwoPoint`` on the optimizer's private RNG (see ``_setup_deap``).

        Draw-for-draw identical to DEAP 1.4's implementation: two ``randint`` calls, the second
        bumped/swapped the same way, then an in-place slice exchange.
        """
        size = min(len(ind1), len(ind2))
        cxpoint1 = self._rng.randint(1, size)
        cxpoint2 = self._rng.randint(1, size - 1)
        if cxpoint2 >= cxpoint1:
            cxpoint2 += 1
        else:
            cxpoint1, cxpoint2 = cxpoint2, cxpoint1

        ind1[cxpoint1:cxpoint2], ind2[cxpoint1:cxpoint2] = (
            ind2[cxpoint1:cxpoint2], ind1[cxpoint1:cxpoint2])
        return ind1, ind2

    def _sel_tournament(self, individuals: List, k: int, tournsize: int,
                        fit_attr: str = "fitness") -> List:
        """``tools.selTournament`` on the optimizer's private RNG (see ``_setup_deap``).

        Draw-for-draw identical to DEAP 1.4: ``k`` tournaments, each ``tournsize`` uniform
        ``choice`` draws with replacement, winner by ``max`` (which keeps the FIRST maximum, as
        DEAP's does). Returns references into *individuals*, not copies -- the caller clones.
        """
        from operator import attrgetter
        chosen = []
        for _ in range(k):
            aspirants = [self._rng.choice(individuals) for _ in range(tournsize)]
            chosen.append(max(aspirants, key=attrgetter(fit_attr)))
        return chosen

    def _create_individual(self) -> List:
        """Create a random individual (chromosome)."""
        individual = []
        for i, (param_name, config) in enumerate(self.param_ranges.items()):
            if config['type'] == 'int':
                value = self._rng.randint(config['min'], config['max'])
            elif config['type'] == 'choice':
                # Categorical gene: encoded as an int INDEX into config['choices']
                # (decode_individual maps it back to the choice value, e.g. a target_price_type
                # string). The GA evolves the index; min/max are 0..len-1.
                value = self._rng.randint(0, len(config['choices']) - 1)
            else:
                value = self._rng.uniform(config['min'], config['max'])
            individual.append(value)
        return creator.Individual(individual)

    def _mutate_individual(self, individual: List, indpb: float = 0.2) -> Tuple[List]:
        """
        Mutate an individual with probability indpb for each gene.

        Args:
            individual: Individual to mutate
            indpb: Independent probability for each gene

        Returns:
            Mutated individual (tuple for DEAP compatibility)
        """
        for i, (param_name, config) in enumerate(self.param_ranges.items()):
            if self._rng.random() < indpb:
                if config['type'] == 'choice':
                    # Categorical: nudge the int index, clamped to 0..len-1.
                    n = len(config['choices'])
                    sigma = max(1.0, (n - 1) / 6)
                    individual[i] = int(np.clip(
                        round(individual[i] + self._rng.gauss(0, sigma)), 0, n - 1))
                elif config['type'] == 'int':
                    # Gaussian mutation for integers
                    sigma = (config['max'] - config['min']) / 6
                    individual[i] = int(np.clip(
                        individual[i] + self._rng.gauss(0, sigma),
                        config['min'],
                        config['max']
                    ))
                else:
                    # Gaussian mutation for floats
                    sigma = (config['max'] - config['min']) / 6
                    individual[i] = np.clip(
                        individual[i] + self._rng.gauss(0, sigma),
                        config['min'],
                        config['max']
                    )
        return (individual,)

    def decode_individual(self, individual: List) -> Dict:
        """
        Decode individual (chromosome) to parameter dictionary.

        Combines per-layer hidden dimensions into a single hidden_dim list
        based on n_rnn_layers value.

        Args:
            individual: List of gene values

        Returns:
            Dictionary of parameter names to values
        """
        raw_params = {}
        for i, (param_name, config) in enumerate(self.param_ranges.items()):
            value = individual[i]
            if config['type'] == 'choice':
                # Map the evolved int index back to the categorical VALUE (e.g. the
                # target_price_type string). Clamp defensively to a valid index.
                idx = int(np.clip(round(value), 0, len(config['choices']) - 1))
                value = config['choices'][idx]
            elif config['type'] == 'int':
                # Round to step size
                step = config.get('step', 1)
                value = int(round(value / step) * step)
            else:
                # Round to step size
                step = config.get('step', 0.01)
                value = round(value / step) * step
            raw_params[param_name] = value

        # Combine per-layer hidden dims into a list
        params = {}
        n_layers = raw_params.get('n_rnn_layers', 2)
        hidden_dims = []

        for key, value in raw_params.items():
            if key.startswith('hidden_dim_layer_'):
                layer_num = int(key.split('_')[-1])
                if layer_num <= n_layers:
                    hidden_dims.append((layer_num, value))
            elif key.startswith('layer_widths_layer_'):
                # For N-BEATS models
                layer_num = int(key.split('_')[-1])
                # Use num_stacks for NBEATS (not num_layers which is for FC layers per block)
                num_stacks = raw_params.get('num_stacks', raw_params.get('num_layers', 30))
                if layer_num <= num_stacks:
                    hidden_dims.append((layer_num, value))
            else:
                params[key] = value

        # Sort by layer number and extract values
        if hidden_dims:
            hidden_dims.sort(key=lambda x: x[0])
            hidden_dim_list = [v for _, v in hidden_dims]
            # Use 'hidden_dim' for RNN/LSTM models (can be list or tuple)
            params['hidden_dim'] = tuple(hidden_dim_list)
            # Also provide as 'layer_widths' for N-BEATS models
            params['layer_widths'] = hidden_dim_list

        return params

    def encode_params(self, params: Dict) -> List:
        """
        Encode parameter dictionary to individual (chromosome).

        Expands hidden_dim list to per-layer parameters.

        Args:
            params: Dictionary of parameter values (hidden_dim can be list/tuple or int)

        Returns:
            List of gene values
        """
        # Expand hidden_dim list to per-layer params if needed
        expanded_params = params.copy()
        hidden_dim = params.get('hidden_dim')
        if isinstance(hidden_dim, (list, tuple)):
            for i, dim in enumerate(hidden_dim):
                expanded_params[f'hidden_dim_layer_{i+1}'] = dim
            # Fill remaining layers with last value
            for i in range(len(hidden_dim), self.MAX_LAYERS):
                expanded_params[f'hidden_dim_layer_{i+1}'] = hidden_dim[-1] if hidden_dim else 64

        # Handle layer_widths similarly (for N-BEATS)
        layer_widths = params.get('layer_widths')
        if isinstance(layer_widths, (list, tuple)) and 'hidden_dim' not in params:
            for i, width in enumerate(layer_widths):
                expanded_params[f'hidden_dim_layer_{i+1}'] = width
            for i in range(len(layer_widths), self.MAX_LAYERS):
                expanded_params[f'hidden_dim_layer_{i+1}'] = layer_widths[-1] if layer_widths else 256

        individual = []
        for param_name in self.param_names:
            config = self.param_ranges[param_name]
            if param_name in expanded_params and config['type'] == 'choice':
                # Categorical genes are chromosome-encoded as an int INDEX (see
                # _create_individual/_mutate_individual/decode_individual) -- convert the
                # decoded VALUE (e.g. a target_price_type string) back to its index. An
                # unrecognised value (e.g. the source came from a differently-configured
                # param space) falls back to index 0 rather than raising.
                choices = config['choices']
                value = expanded_params[param_name]
                value = choices.index(value) if value in choices else 0
            else:
                value = expanded_params.get(param_name, config['min'])
            individual.append(value)
        return creator.Individual(individual)

    def resume_from_checkpoint(self, checkpoint: Dict) -> tuple:
        """
        Resume optimization from saved checkpoint.

        Args:
            checkpoint: Saved checkpoint data containing population, generation, etc.

        Returns:
            Tuple of (start_generation, population_data, fitnesses)
        """
        self.history = checkpoint.get('history', [])
        self.best_fitness = checkpoint.get('best_fitness')
        self.best_individual = checkpoint.get('best_individual')

        # Restore Python random state if available. tuple() alone is NOT enough: the checkpoint
        # is a JSON column, so the inner 625-int state vector comes back as a list and
        # setstate() rejects it ("state vector must be a tuple") -- which used to be swallowed
        # into the warning below, leaving the resumed run on an un-restored RNG.
        if 'random_state' in checkpoint:
            try:
                random.setstate(_jsonable_to_py_state(checkpoint['random_state']))
            except Exception as e:
                logger.warning(f"Could not restore random state: {e}")

        # Restore numpy random state if available (backward-compatible: older
        # checkpoints lack np_random_state and simply skip this restore).
        if 'np_random_state' in checkpoint:
            try:
                np.random.set_state(_jsonable_to_np_state(checkpoint['np_random_state']))
            except Exception as e:
                logger.warning(f"Could not restore numpy random state: {e}")

        # Restore the GA's OWN generator -- the one that actually decides the rest of the search
        # (see self._rng). Without this a resume would carry on from a construction-time clone of
        # whatever the global RNG happened to be, i.e. from the wrong point in the sequence.
        #
        # LEGACY checkpoints (written while the GA still drew from the global module) have no
        # 'ga_random_state'; re-cloning the global state we just restored above reproduces what
        # those runs would have continued with exactly, so an in-flight resume is unaffected.
        #
        # Always MUTATE self._rng in place rather than rebinding it: the toolbox's ``attr_i``
        # generators are bound methods of this exact object.
        if 'ga_random_state' in checkpoint:
            try:
                self._rng.setstate(_jsonable_to_py_state(checkpoint['ga_random_state']))
            except Exception as e:
                logger.warning(f"Could not restore GA random state: {e}")
                self._rng.setstate(random.getstate())
        else:
            self._rng.setstate(random.getstate())

        logger.info(f"Resuming from generation {checkpoint.get('generation', 0)}")
        # partial=True means the stored generation was INTERRUPTED mid-evaluation, so resume INTO
        # it rather than after it. The fitness list carries which individuals are already done.
        next_gen = checkpoint.get('generation', 0)
        if not checkpoint.get('partial'):
            next_gen += 1
        return next_gen, checkpoint.get('population', []), checkpoint.get('fitnesses')

    def _maybe_partial_checkpoint(self, gen: int, population: list, checkpoint_callback) -> None:
        """Checkpoint mid-generation every ``partial_checkpoint_every`` completed trials.

        Best-effort: a checkpoint write must never fail an otherwise healthy generation.
        """
        if not self.partial_checkpoint_every:
            return
        self._partial_counter += 1
        if self._partial_counter % self.partial_checkpoint_every:
            return
        try:
            checkpoint_callback(gen, population, partial=True)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"partial checkpoint failed (ignored): {e}")

    def rebuild_population(self, genes: list, fitnesses: list = None) -> list:
        """Rebuild a DEAP population from checkpointed genes + fitness values.

        A ``None`` fitness (or a missing/short list) leaves that individual INVALID, which is
        precisely the signal DEAP's ``invalid_ind`` filter acts on -- so an unevaluated
        individual is re-run and an evaluated one is not. Never raises on a malformed list:
        the worst outcome must be re-evaluating something, never crashing a resume.
        """
        population = []
        for i, g in enumerate(genes):
            ind = creator.Individual(g)
            fit = None
            if fitnesses is not None and i < len(fitnesses):
                fit = fitnesses[i]
            if fit is not None:
                try:
                    ind.fitness.values = (float(fit),)
                except (TypeError, ValueError):
                    pass          # unusable value -> leave invalid, it will be re-evaluated
            population.append(ind)
        return population

    def get_checkpoint_data(self, generation: int, population: list) -> Dict:
        """
        Get current state for checkpointing.

        Args:
            generation: Current generation number
            population: Current population

        Returns:
            Checkpoint data dict
        """
        return {
            'generation': generation,
            'population': [list(ind) for ind in population],
            # Fitness per individual, index-aligned with 'population'. None where the individual
            # has not been evaluated -- which is what makes a MID-GENERATION checkpoint possible:
            # on resume, DEAP's invalid-fitness filter re-runs exactly the Nones.
            'fitnesses': [
                ind.fitness.values[0] if ind.fitness.valid else None
                for ind in population
            ],
            'best_individual': list(self.best_individual) if self.best_individual else None,
            'best_fitness': self.best_fitness,
            'history': self.history,
            'random_state': _py_state_to_jsonable(random.getstate()),
            'np_random_state': _np_state_to_jsonable(np.random.get_state()),
            # The GA's OWN generator (self._rng) -- the only one whose position decides the rest
            # of the search. The two above are the PROCESS globals, kept because a fitness
            # function may legitimately depend on them; this one is what makes a resume land on
            # the same offspring the uninterrupted run would have produced.
            'ga_random_state': _py_state_to_jsonable(self._rng.getstate()),
        }

    def optimize(
        self,
        fitness_function: Callable[[Dict], float],
        callback: Callable[[int, float, Dict], None] = None,
        start_generation: int = 0,
        initial_population: list = None,
        restored_fitnesses: list = None,
        checkpoint_callback: Callable[[int, list], None] = None,
        on_generation_start: Callable[[int], None] = None,
        batch_fitness: Callable[[list], list] = None
    ) -> Dict:
        """
        Run genetic algorithm optimization.

        Args:
            fitness_function: Function that takes params dict and returns fitness score
            callback: Optional callback(generation, best_fitness, best_params) called after each generation
            start_generation: Generation to start from (for resume)
            initial_population: Initial population data (for resume)
            restored_fitnesses: Fitness per restored individual, index-aligned with
                initial_population. None entries stay INVALID and are re-evaluated.
            checkpoint_callback: Called with (gen, population, partial=False) for saving
            on_generation_start: Optional callback(generation) called before evaluating each generation

        Returns:
            Dictionary with best parameters and optimization history
        """
        logger.info(f"Starting genetic optimization: pop={self.population_size}, gen={self.n_generations}, start_gen={start_generation}")

        # Create or restore population
        if initial_population:
            population = self.rebuild_population(initial_population, restored_fitnesses)
            n_valid = sum(1 for ind in population if ind.fitness.valid)
            logger.info(
                f"Restored population of {len(population)} individuals "
                f"({n_valid} already evaluated, {len(population) - n_valid} to run)")
        else:
            population = self.toolbox.population(n=self.population_size)

        # Evaluate fitness function wrapper
        def evaluate(individual):
            params = self.decode_individual(individual)
            try:
                fitness = fitness_function(params)
                return (fitness,)
            except Exception as e:
                logger.warning(f"Fitness evaluation failed: {e}")
                return (0.0,)

        self.toolbox.register("evaluate", evaluate)

        # Statistics tracking
        stats = tools.Statistics(lambda ind: ind.fitness.values)
        stats.register("avg", np.mean)
        stats.register("max", np.max)
        stats.register("min", np.min)

        # Track best fitness for early stopping
        best_fitness_history = []
        no_improvement_count = 0

        # Restore best fitness history from resumed state
        if self.history:
            best_fitness_history = [h['best_fitness'] for h in self.history]

        # Evolution loop
        for gen in range(start_generation, self.n_generations):
            # Notify generation start before evaluations
            if on_generation_start:
                on_generation_start(gen)

            # Only evaluate individuals whose fitness is invalid (not elites)
            # This prevents re-evaluating elites which would give different results
            # due to stochastic neural network training
            invalid_ind = [ind for ind in population if not ind.fitness.valid]
            # Fresh throttle phase per generation: a carried-over counter would put this
            # generation's first partial write at an arbitrary offset.
            self._partial_counter = 0

            if batch_fitness is not None:
                # TRUE multiprocessing path: the caller evaluates the whole batch of
                # invalid individuals at once (decoded param dicts -> fitnesses), running the
                # CPU-bound work in worker PROCESSES (no GIL). All shared state (memo /
                # bookkeeping / DB) stays in the caller's main process. Order is preserved.
                param_dicts = [self.decode_individual(ind) for ind in invalid_ind]

                def _on_result(idx: int, fit: float, _inv=invalid_ind, _gen=gen):
                    """Assign fitness the moment a trial lands so the population is
                    checkpointable mid-generation. Guarded: a bad index must not kill the run."""
                    try:
                        _inv[idx].fitness.values = (float(fit),)
                    except (IndexError, TypeError, ValueError):
                        return
                    if checkpoint_callback:
                        self._maybe_partial_checkpoint(_gen, population, checkpoint_callback)

                # Not every batch_fitness accepts on_result -- brute force and the ML GA supply
                # one-argument callables. Probe rather than assume: a TypeError here would be
                # indistinguishable from one raised INSIDE the evaluation.
                if param_dicts:
                    fits = (batch_fitness(param_dicts, on_result=_on_result)
                            if _accepts_on_result(batch_fitness)
                            else batch_fitness(param_dicts))
                else:
                    fits = []
                fitnesses = [(float(f),) for f in fits]
            elif self.parallel_individuals > 1:
                # Thread pool — only useful for I/O-bound or GPU work (the ML engine), NOT for
                # CPU-bound daily backtests (GIL-serialised). The daily path uses batch_fitness.
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=self.parallel_individuals) as executor:
                    fitnesses = list(executor.map(self.toolbox.evaluate, invalid_ind))
            else:
                fitnesses = list(map(self.toolbox.evaluate, invalid_ind))

            for ind, fit in zip(invalid_ind, fitnesses):
                ind.fitness.values = fit

            # Record statistics
            record = stats.compile(population)
            logger.info(f"Gen {gen}: avg={record['avg']:.4f}, max={record['max']:.4f}")

            # Track best individual
            best_ind = tools.selBest(population, 1)[0]
            best_fit = best_ind.fitness.values[0]
            best_params = self.decode_individual(best_ind)

            self.history.append({
                'generation': gen,
                'best_fitness': best_fit,
                'best_params': best_params,
                'stats': record
            })

            # Call callback if provided
            if callback:
                callback(gen, best_fit, best_params)

            # Save checkpoint after each generation
            if checkpoint_callback:
                checkpoint_callback(gen, population)

            # Update best overall and track early stopping
            if self.best_fitness is None or best_fit > self.best_fitness:
                self.best_fitness = best_fit
                self.best_individual = list(best_ind)
                no_improvement_count = 0
            else:
                no_improvement_count += 1

            # Early stopping: stop if overall best hasn't improved for N generations
            if no_improvement_count >= self.early_stopping_generations:
                logger.info(
                    f"Early stopping at generation {gen} — no improvement for "
                    f"{no_improvement_count} generations (best={self.best_fitness:.4f})"
                )
                break

            best_fitness_history.append(best_fit)

            # ELITISM: Preserve the best individuals unchanged
            n_elite = max(1, int((self.elitism_percent / 100.0) * len(population)))
            elites = tools.selBest(population, n_elite)
            # Clone elites to preserve them unchanged
            elites = [self.toolbox.clone(ind) for ind in elites]

            logger.debug(f"Gen {gen}: Preserving {n_elite} elite individuals (best fitness: {elites[0].fitness.values[0]:.4f})")

            # Selection and reproduction for the remaining slots
            n_offspring = len(population) - n_elite
            offspring = self.toolbox.select(population, n_offspring)
            offspring = list(map(self.toolbox.clone, offspring))

            # Crossover
            for child1, child2 in zip(offspring[::2], offspring[1::2]):
                if self._rng.random() < self.crossover_prob:
                    self.toolbox.mate(child1, child2)
                    del child1.fitness.values
                    del child2.fitness.values

            # Mutation
            for mutant in offspring:
                if self._rng.random() < self.mutation_prob:
                    self.toolbox.mutate(mutant)
                    del mutant.fitness.values

            # Combine elites (unchanged) with new offspring
            population[:] = elites + offspring

        # Final best
        best_params = self.decode_individual(self.best_individual)

        logger.info(f"Optimization complete. Best fitness: {self.best_fitness:.4f}")
        logger.info(f"Best params: {best_params}")

        return {
            'best_params': best_params,
            'best_fitness': self.best_fitness,
            'generations_run': len(self.history),
            'history': self.history
        }

    def get_progress(self) -> Dict:
        """
        Get current optimization progress.

        Returns:
            Progress information
        """
        return {
            'generations_completed': len(self.history),
            'total_generations': self.n_generations,
            'best_fitness': self.best_fitness,
            'best_params': self.decode_individual(self.best_individual) if self.best_individual else None,
            'history': self.history[-5:] if self.history else []  # Last 5 generations
        }


class FitnessEvaluator:
    """
    Fitness function implementations for genetic optimization.
    """

    @staticmethod
    def create_model_fitness(
        train_fn: Callable,
        eval_fn: Callable,
        train_data: Any,
        test_data: Any,
        metric: str = 'accuracy'
    ) -> Callable:
        """
        Create a fitness function for model optimization.

        Args:
            train_fn: Function to train model with params
            eval_fn: Function to evaluate model
            train_data: Training data
            test_data: Test data
            metric: Metric to optimize ('accuracy', 'mape', etc.)

        Returns:
            Fitness function that takes params and returns score
        """
        def fitness(params: Dict) -> float:
            try:
                # Train model
                model = train_fn(params, train_data)

                # Evaluate
                metrics = eval_fn(model, test_data)

                # Get fitness score
                if metric == 'accuracy':
                    return metrics.get('accuracy', 0.0)
                elif metric == 'mape':
                    # Lower MAPE is better, so invert
                    mape = metrics.get('mape', 100.0)
                    return 1.0 / (1.0 + mape)
                else:
                    return metrics.get(metric, 0.0)

            except Exception as e:
                logger.warning(f"Fitness evaluation error: {e}")
                return 0.0

        return fitness

    @staticmethod
    def dummy_fitness(params: Dict) -> float:
        """
        Dummy fitness function for testing.

        Args:
            params: Model parameters

        Returns:
            Fitness score based on parameter values
        """
        # Simple fitness based on some parameter heuristics
        score = 0.5

        # Prefer moderate hidden dims (handle both list and scalar)
        hidden_dim = params.get('hidden_dim', 64)
        if isinstance(hidden_dim, (list, tuple)):
            # Score based on average layer size
            avg_dim = sum(hidden_dim) / len(hidden_dim) if hidden_dim else 64
            if 64 <= avg_dim <= 128:
                score += 0.15
            # Bonus for decreasing layer sizes (common architecture pattern)
            if len(hidden_dim) > 1 and all(hidden_dim[i] >= hidden_dim[i+1] for i in range(len(hidden_dim)-1)):
                score += 0.05
        else:
            if 64 <= hidden_dim <= 128:
                score += 0.2

        # Prefer 2 layers
        n_layers = params.get('n_rnn_layers', 2)
        if n_layers == 2:
            score += 0.15

        # Prefer moderate dropout
        dropout = params.get('dropout', 0.1)
        if 0.1 <= dropout <= 0.3:
            score += 0.15

        # Add some noise
        score += random.uniform(-0.1, 0.1)

        return max(0.0, min(1.0, score))
