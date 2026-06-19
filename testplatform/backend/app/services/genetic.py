"""
Genetic Optimization Service

Implements genetic algorithm optimization for model hyperparameters
using DEAP (Distributed Evolutionary Algorithms in Python).
"""

import numpy as np
from datetime import datetime
from typing import Dict, List, Any, Optional, Callable, Tuple
import logging
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

        self.toolbox = None
        self.best_individual = None
        self.best_fitness = None
        self.history = []

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
                    random.randint,
                    config['min'],
                    config['max']
                )
            else:
                self.toolbox.register(
                    f"attr_{i}",
                    random.uniform,
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

        # Register genetic operators
        self.toolbox.register("mate", tools.cxTwoPoint)
        self.toolbox.register("mutate", self._mutate_individual)
        self.toolbox.register("select", tools.selTournament, tournsize=3)

    def _create_individual(self) -> List:
        """Create a random individual (chromosome)."""
        individual = []
        for i, (param_name, config) in enumerate(self.param_ranges.items()):
            if config['type'] == 'int':
                value = random.randint(config['min'], config['max'])
            elif config['type'] == 'choice':
                # Categorical gene: encoded as an int INDEX into config['choices']
                # (decode_individual maps it back to the choice value, e.g. a target_price_type
                # string). The GA evolves the index; min/max are 0..len-1.
                value = random.randint(0, len(config['choices']) - 1)
            else:
                value = random.uniform(config['min'], config['max'])
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
            if random.random() < indpb:
                if config['type'] == 'choice':
                    # Categorical: nudge the int index, clamped to 0..len-1.
                    n = len(config['choices'])
                    sigma = max(1.0, (n - 1) / 6)
                    individual[i] = int(np.clip(
                        round(individual[i] + random.gauss(0, sigma)), 0, n - 1))
                elif config['type'] == 'int':
                    # Gaussian mutation for integers
                    sigma = (config['max'] - config['min']) / 6
                    individual[i] = int(np.clip(
                        individual[i] + random.gauss(0, sigma),
                        config['min'],
                        config['max']
                    ))
                else:
                    # Gaussian mutation for floats
                    sigma = (config['max'] - config['min']) / 6
                    individual[i] = np.clip(
                        individual[i] + random.gauss(0, sigma),
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
            individual.append(expanded_params.get(param_name, self.param_ranges[param_name]['min']))
        return creator.Individual(individual)

    def resume_from_checkpoint(self, checkpoint: Dict) -> tuple:
        """
        Resume optimization from saved checkpoint.

        Args:
            checkpoint: Saved checkpoint data containing population, generation, etc.

        Returns:
            Tuple of (start_generation, population_data)
        """
        self.history = checkpoint.get('history', [])
        self.best_fitness = checkpoint.get('best_fitness')
        self.best_individual = checkpoint.get('best_individual')

        # Restore random state if available
        if 'random_state' in checkpoint:
            try:
                random.setstate(tuple(checkpoint['random_state']))
            except Exception as e:
                logger.warning(f"Could not restore random state: {e}")

        # Restore numpy random state if available (backward-compatible: older
        # checkpoints lack np_random_state and simply skip this restore).
        if 'np_random_state' in checkpoint:
            try:
                np.random.set_state(_jsonable_to_np_state(checkpoint['np_random_state']))
            except Exception as e:
                logger.warning(f"Could not restore numpy random state: {e}")

        logger.info(f"Resuming from generation {checkpoint.get('generation', 0)}")
        return checkpoint.get('generation', 0) + 1, checkpoint.get('population', [])

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
            'best_individual': list(self.best_individual) if self.best_individual else None,
            'best_fitness': self.best_fitness,
            'history': self.history,
            'random_state': list(random.getstate()),
            'np_random_state': _np_state_to_jsonable(np.random.get_state()),
        }

    def optimize(
        self,
        fitness_function: Callable[[Dict], float],
        callback: Callable[[int, float, Dict], None] = None,
        start_generation: int = 0,
        initial_population: list = None,
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
            checkpoint_callback: Called after each generation with (gen, population) for saving
            on_generation_start: Optional callback(generation) called before evaluating each generation

        Returns:
            Dictionary with best parameters and optimization history
        """
        logger.info(f"Starting genetic optimization: pop={self.population_size}, gen={self.n_generations}, start_gen={start_generation}")

        # Create or restore population
        if initial_population:
            population = [creator.Individual(ind) for ind in initial_population]
            logger.info(f"Restored population of {len(population)} individuals")
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

            if batch_fitness is not None:
                # TRUE multiprocessing path: the caller evaluates the whole batch of
                # invalid individuals at once (decoded param dicts -> fitnesses), running the
                # CPU-bound work in worker PROCESSES (no GIL). All shared state (memo /
                # bookkeeping / DB) stays in the caller's main process. Order is preserved.
                param_dicts = [self.decode_individual(ind) for ind in invalid_ind]
                fits = batch_fitness(param_dicts) if param_dicts else []
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
                if random.random() < self.crossover_prob:
                    self.toolbox.mate(child1, child2)
                    del child1.fitness.values
                    del child2.fitness.values

            # Mutation
            for mutant in offspring:
                if random.random() < self.mutation_prob:
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
