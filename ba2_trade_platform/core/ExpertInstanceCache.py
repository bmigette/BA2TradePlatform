"""
Expert Instance Cache with Singleton Pattern and Settings Caching

This module provides a thread-safe singleton cache for expert instances with cached settings
to dramatically reduce database calls when accessing expert settings.

Key features:
- Singleton pattern: Only one instance per expert_instance_id
- Settings caching: Settings cached at instance level
- Thread-safe: All operations use locks for thread safety
- Automatic invalidation: Cache can be invalidated when settings change
"""

import threading
from typing import Dict, Any, Optional
from ..logger import logger


class ExpertInstanceCache:
    """
    Thread-safe singleton cache for expert instances.
    
    This cache ensures:
    1. Only one instance per expert_instance_id exists in memory
    2. Thread-safe access using locks
    3. Cache invalidation when needed
    """
    
    _lock = threading.Lock()  # Lock for cache dictionary access
    _cache: Dict[int, Any] = {}  # expert_instance_id -> expert instance
    
    @classmethod
    def get_instance(cls, expert_instance_id: int, expert_class, force_new: bool = False):
        """
        Get a cached expert instance or create a new one if not cached.
        
        This method implements the singleton pattern: only one instance per expert_instance_id
        exists in memory. Subsequent calls return the same instance.
        
        Args:
            expert_instance_id: The expert instance ID
            expert_class: The expert class to instantiate if not cached
            force_new: If True, create a new instance even if cached (invalidates cache)
            
        Returns:
            Expert instance (singleton per expert_instance_id)
        """
        with cls._lock:
            # Check if instance exists and force_new is not set
            if not force_new and expert_instance_id in cls._cache:
                #logger.debug(f"Returning cached expert instance for expert {expert_instance_id}")
                return cls._cache[expert_instance_id]
            
            # Create new instance
            logger.debug(f"Creating new expert instance for expert {expert_instance_id}")
            instance = expert_class(expert_instance_id)
            
            # Cache the instance
            cls._cache[expert_instance_id] = instance
            
            return instance
    
    @classmethod
    def invalidate_instance(cls, expert_instance_id: int):
        """
        Invalidate cached instance for an expert.
        
        Call this method to force recreation of the expert instance on next access.
        
        Args:
            expert_instance_id: The expert instance ID whose instance should be invalidated
        """
        with cls._lock:
            if expert_instance_id in cls._cache:
                logger.info(f"Invalidating instance cache for expert {expert_instance_id}")
                # Clear cached settings on the instance before removing it
                instance = cls._cache[expert_instance_id]
                if hasattr(instance, '_settings_cache'):
                    instance._settings_cache = None
                del cls._cache[expert_instance_id]
    
    @classmethod
    def clear_cache(cls):
        """
        Clear all cached expert instances.
        
        Use this method sparingly, typically only for testing or when you need
        to force reload all expert instances.
        """
        with cls._lock:
            # Clear settings cache on all instances before clearing
            for instance in cls._cache.values():
                if hasattr(instance, '_settings_cache'):
                    instance._settings_cache = None
            cls._cache.clear()
            logger.info("Cleared all expert instance cache")
    
    @classmethod
    def get_cache_stats(cls) -> Dict[str, int]:
        """
        Get statistics about the cache.
        
        Returns:
            Dict with cache statistics:
                - cached_instances: Number of expert instances currently cached
                - expert_instance_ids: List of cached expert instance IDs
        """
        with cls._lock:
            return {
                "cached_instances": len(cls._cache),
                "expert_instance_ids": list(cls._cache.keys())
            }
