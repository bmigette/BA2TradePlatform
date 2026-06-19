"""
Genetic Optimization Abstraction Layer

Provides a common interface for different genetic optimization libraries
including DEAP, PyGAD, and shinkaEvolve.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional, Callable, Tuple
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class GeneticLibrary(str, Enum):
    """Supported genetic optimization libraries."""
    DEAP = "deap"
    PYGAD = "pygad"
    SHINKA_EVOLVE = "shinka_evolve"


class OptimizationResult:
    """Standard result format from genetic optimization."""

    def __init__(
        self,
        best_params: Dict[str, Any],
        best_fitness: float,
        generations_run: int,
        history: List[Dict],
        library: GeneticLibrary,
        converged: bool = False,
        early_stopped: bool = False
    ):
        self.best_params = best_params
        self.best_fitness = best_fitness
        self.generations_run = generations_run
        self.history = history
        self.library = library
        self.converged = converged
        self.early_stopped = early_stopped

    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization."""
        return {
            "best_params": self.best_params,
            "best_fitness": self.best_fitness,
            "generations_run": self.generations_run,
            "history": self.history,
            "library": self.library.value,
            "converged": self.converged,
            "early_stopped": self.early_stopped
        }


class GeneticOptimizerBase(ABC):
    """
    Abstract base class for genetic optimization.

    All genetic library adapters must implement this interface.
    """

    # Maximum number of layers for per-layer optimization
    MAX_LAYERS = 4

    # Standard hyperparameter ranges
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
        elitism_percent: float = 10.0
    ):
        """
        Initialize the optimizer with configuration.

        Args:
            param_ranges: Dictionary of parameter ranges to optimize
            population_size: Number of individuals in population
            n_generations: Number of generations to evolve
            crossover_prob: Probability of crossover
            mutation_prob: Probability of mutation
            early_stopping_generations: Stop if no improvement for this many generations
            elitism_percent: Percentage of best individuals to preserve unchanged
        """
        self.param_ranges = param_ranges or self.DEFAULT_PARAM_RANGES
        self.population_size = population_size
        self.n_generations = n_generations
        self.crossover_prob = crossover_prob
        self.mutation_prob = mutation_prob
        self.early_stopping_generations = early_stopping_generations
        self.elitism_percent = elitism_percent

        self.best_individual = None
        self.best_fitness = None
        self.history = []

    @property
    @abstractmethod
    def library(self) -> GeneticLibrary:
        """Return the library type."""
        pass

    @property
    @abstractmethod
    def is_available(self) -> bool:
        """Check if the underlying library is available."""
        pass

    @abstractmethod
    def initialize(self) -> None:
        """
        Initialize the optimizer with the current configuration.

        This should set up the internal state of the optimizer.
        """
        pass

    @abstractmethod
    def optimize(
        self,
        fitness_function: Callable[[Dict], float],
        callback: Callable[[int, float, Dict], None] = None
    ) -> OptimizationResult:
        """
        Run the genetic optimization.

        Args:
            fitness_function: Function that takes params dict and returns fitness score
            callback: Optional callback(generation, best_fitness, best_params)

        Returns:
            OptimizationResult with best parameters and history
        """
        pass

    @abstractmethod
    def get_progress(self) -> Dict:
        """
        Get current optimization progress.

        Returns:
            Progress information
        """
        pass

    @abstractmethod
    def decode_individual(self, individual: List) -> Dict:
        """
        Decode individual (chromosome) to parameter dictionary.

        Args:
            individual: List of gene values

        Returns:
            Dictionary of parameter names to values
        """
        pass

    @abstractmethod
    def encode_params(self, params: Dict) -> List:
        """
        Encode parameter dictionary to individual (chromosome).

        Args:
            params: Dictionary of parameter values

        Returns:
            List of gene values
        """
        pass


class GeneticOptimizerFactory:
    """
    Factory for creating genetic optimizers.

    Handles library selection and availability checking.
    """

    _adapters: Dict[GeneticLibrary, type] = {}

    @classmethod
    def register(cls, library: GeneticLibrary, adapter_class: type) -> None:
        """
        Register an adapter class for a library.

        Args:
            library: The library type
            adapter_class: The adapter class implementing GeneticOptimizerBase
        """
        cls._adapters[library] = adapter_class
        logger.info(f"Registered genetic optimizer adapter for {library.value}")

    @classmethod
    def get_available_libraries(cls) -> List[Dict]:
        """
        Get list of available genetic optimization libraries.

        Returns:
            List of library info dictionaries
        """
        available = []
        for library, adapter_class in cls._adapters.items():
            try:
                adapter = adapter_class()
                available.append({
                    "library": library.value,
                    "available": adapter.is_available,
                    "description": cls._get_library_description(library)
                })
            except Exception as e:
                available.append({
                    "library": library.value,
                    "available": False,
                    "error": str(e)
                })

        return available

    @classmethod
    def create(
        cls,
        library: GeneticLibrary = None,
        **kwargs
    ) -> GeneticOptimizerBase:
        """
        Create a genetic optimizer for the specified library.

        Args:
            library: The library to use (auto-selects if None)
            **kwargs: Configuration parameters for the optimizer

        Returns:
            Configured GeneticOptimizerBase instance

        Raises:
            ValueError: If requested library is not available
        """
        if library is None:
            # Auto-select first available library
            for lib, adapter_class in cls._adapters.items():
                try:
                    adapter = adapter_class(**kwargs)
                    if adapter.is_available:
                        logger.info(f"Auto-selected genetic library: {lib.value}")
                        adapter.initialize()
                        return adapter
                except Exception:
                    continue

            raise ValueError("No genetic optimization library available")

        if library not in cls._adapters:
            raise ValueError(
                f"Unknown library: {library}. Available: {list(cls._adapters.keys())}"
            )

        adapter = cls._adapters[library](**kwargs)
        if not adapter.is_available:
            raise ValueError(f"Library {library.value} is not available")

        adapter.initialize()
        return adapter

    @staticmethod
    def _get_library_description(library: GeneticLibrary) -> str:
        """Get description for a library."""
        descriptions = {
            GeneticLibrary.DEAP: "Distributed Evolutionary Algorithms in Python - Flexible and powerful GA toolkit",
            GeneticLibrary.PYGAD: "PyGAD - Easy-to-use genetic algorithm library with NumPy integration",
            GeneticLibrary.SHINKA_EVOLVE: "shinkaEvolve - GPU-accelerated evolutionary optimization"
        }
        return descriptions.get(library, "Unknown library")


# Register the DEAP adapter
def _register_deap_adapter():
    """Register the DEAP adapter if available."""
    try:
        from app.services.genetic import GeneticOptimizer, DEAP_AVAILABLE

        class DEAPAdapter(GeneticOptimizerBase):
            """Adapter for DEAP library."""

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self._optimizer = None

            @property
            def library(self) -> GeneticLibrary:
                return GeneticLibrary.DEAP

            @property
            def is_available(self) -> bool:
                return DEAP_AVAILABLE

            def initialize(self) -> None:
                if not self.is_available:
                    raise RuntimeError("DEAP library not available")

                self._optimizer = GeneticOptimizer(
                    param_ranges=self.param_ranges,
                    population_size=self.population_size,
                    n_generations=self.n_generations,
                    crossover_prob=self.crossover_prob,
                    mutation_prob=self.mutation_prob,
                    early_stopping_generations=self.early_stopping_generations,
                    elitism_percent=self.elitism_percent
                )

            def optimize(
                self,
                fitness_function: Callable[[Dict], float],
                callback: Callable[[int, float, Dict], None] = None
            ) -> OptimizationResult:
                result = self._optimizer.optimize(fitness_function, callback)

                return OptimizationResult(
                    best_params=result['best_params'],
                    best_fitness=result['best_fitness'],
                    generations_run=result['generations_run'],
                    history=result['history'],
                    library=self.library,
                    early_stopped=len(result['history']) < self.n_generations
                )

            def get_progress(self) -> Dict:
                return self._optimizer.get_progress()

            def decode_individual(self, individual: List) -> Dict:
                return self._optimizer.decode_individual(individual)

            def encode_params(self, params: Dict) -> List:
                return self._optimizer.encode_params(params)

        GeneticOptimizerFactory.register(GeneticLibrary.DEAP, DEAPAdapter)

    except ImportError:
        logger.warning("DEAP adapter could not be registered")


# Register adapters on module load
_register_deap_adapter()


# Placeholder for PyGAD adapter
class PyGADAdapter(GeneticOptimizerBase):
    """Adapter for PyGAD library (placeholder)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._pygad = None

    @property
    def library(self) -> GeneticLibrary:
        return GeneticLibrary.PYGAD

    @property
    def is_available(self) -> bool:
        try:
            import pygad
            return True
        except ImportError:
            return False

    def initialize(self) -> None:
        if not self.is_available:
            raise RuntimeError("PyGAD library not available. Install with: pip install pygad")
        import pygad
        self._pygad = pygad

    def optimize(
        self,
        fitness_function: Callable[[Dict], float],
        callback: Callable[[int, float, Dict], None] = None
    ) -> OptimizationResult:
        # PyGAD implementation would go here
        raise NotImplementedError("PyGAD adapter not yet fully implemented")

    def get_progress(self) -> Dict:
        return {
            "generations_completed": len(self.history),
            "total_generations": self.n_generations,
            "best_fitness": self.best_fitness
        }

    def decode_individual(self, individual: List) -> Dict:
        params = {}
        for i, (param_name, config) in enumerate(self.param_ranges.items()):
            value = individual[i]
            if config['type'] == 'int':
                step = config.get('step', 1)
                value = int(round(value / step) * step)
            else:
                step = config.get('step', 0.01)
                value = round(value / step) * step
            params[param_name] = value
        return params

    def encode_params(self, params: Dict) -> List:
        return [params.get(name, self.param_ranges[name]['min'])
                for name in self.param_ranges.keys()]


# Placeholder for shinkaEvolve adapter
class ShinkaEvolveAdapter(GeneticOptimizerBase):
    """Adapter for shinkaEvolve library (placeholder)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._shinka = None

    @property
    def library(self) -> GeneticLibrary:
        return GeneticLibrary.SHINKA_EVOLVE

    @property
    def is_available(self) -> bool:
        try:
            import shinka_evolve
            return True
        except ImportError:
            return False

    def initialize(self) -> None:
        if not self.is_available:
            raise RuntimeError(
                "shinkaEvolve library not available. Install with: pip install shinka-evolve"
            )
        import shinka_evolve
        self._shinka = shinka_evolve

    def optimize(
        self,
        fitness_function: Callable[[Dict], float],
        callback: Callable[[int, float, Dict], None] = None
    ) -> OptimizationResult:
        # shinkaEvolve implementation would go here
        raise NotImplementedError("shinkaEvolve adapter not yet fully implemented")

    def get_progress(self) -> Dict:
        return {
            "generations_completed": len(self.history),
            "total_generations": self.n_generations,
            "best_fitness": self.best_fitness
        }

    def decode_individual(self, individual: List) -> Dict:
        params = {}
        for i, (param_name, config) in enumerate(self.param_ranges.items()):
            value = individual[i]
            if config['type'] == 'int':
                step = config.get('step', 1)
                value = int(round(value / step) * step)
            else:
                step = config.get('step', 0.01)
                value = round(value / step) * step
            params[param_name] = value
        return params

    def encode_params(self, params: Dict) -> List:
        return [params.get(name, self.param_ranges[name]['min'])
                for name in self.param_ranges.keys()]


# Register placeholder adapters
GeneticOptimizerFactory.register(GeneticLibrary.PYGAD, PyGADAdapter)
GeneticOptimizerFactory.register(GeneticLibrary.SHINKA_EVOLVE, ShinkaEvolveAdapter)
