"""
Account Instance Cache with Singleton Pattern and Settings Caching

This module provides a thread-safe singleton cache for account instances with cached settings
to dramatically reduce database calls when accessing account settings.

Key features:
- Singleton pattern: Only one instance per account_id
- Settings caching: Settings cached at instance level
- Thread-safe: All operations use locks for thread safety
- Automatic invalidation: Cache can be invalidated when settings change
"""

import threading
from typing import Dict, Any, Optional
from ..logger import logger


class AccountInstanceCache:
    """
    Thread-safe singleton cache for account instances.
    
    This cache ensures:
    1. Only one instance per account_id exists in memory
    2. Thread-safe access using locks
    3. Cache invalidation when needed
    """
    
    _lock = threading.Lock()  # Lock for cache dictionary access
    _cache: Dict[int, Any] = {}  # account_id -> account instance
    
    @classmethod
    def get_instance(cls, account_id: int, account_class, force_new: bool = False):
        """
        Get a cached account instance or create a new one if not cached.
        
        This method implements the singleton pattern: only one instance per account_id
        exists in memory. Subsequent calls return the same instance.
        
        Args:
            account_id: The account ID
            account_class: The account class to instantiate if not cached
            force_new: If True, create a new instance even if cached (invalidates cache)
            
        Returns:
            Account instance (singleton per account_id)
        """
        with cls._lock:
            # Check if instance exists and force_new is not set
            if not force_new and account_id in cls._cache:
                logger.debug(f"Returning cached account instance for account {account_id}")
                return cls._cache[account_id]
            
            # Create new instance
            logger.debug(f"Creating new account instance for account {account_id}")
            instance = account_class(account_id)
            
            # Cache the instance
            cls._cache[account_id] = instance
            
            return instance
    

    
    @classmethod
    def invalidate_instance(cls, account_id: int):
        """
        Invalidate cached instance for an account.
        
        Call this method to force recreation of the account instance on next access.
        
        Args:
            account_id: The account ID whose instance should be invalidated
        """
        with cls._lock:
            if account_id in cls._cache:
                logger.info(f"Invalidating instance cache for account {account_id}")
                # Clear cached settings on the instance before removing it
                instance = cls._cache[account_id]
                if hasattr(instance, '_settings_cache'):
                    instance._settings_cache = None
                del cls._cache[account_id]
    
    @classmethod
    def clear_cache(cls):
        """
        Clear all cached instances.
        
        Use this for testing or when you need to ensure all instances are fresh.
        """
        with cls._lock:
            logger.info(f"Clearing all account instance caches ({len(cls._cache)} instances)")
            # Clear cached settings on all instances
            for instance in cls._cache.values():
                if hasattr(instance, '_settings_cache'):
                    instance._settings_cache = None
            cls._cache.clear()
    
    @classmethod
    def get_cache_stats(cls) -> Dict[str, int]:
        """
        Get statistics about the cache.
        
        Returns:
            Dict with cache statistics
        """
        with cls._lock:
            # Count instances with cached settings
            instances_with_cached_settings = sum(
                1 for instance in cls._cache.values() 
                if hasattr(instance, '_settings_cache') and instance._settings_cache is not None
            )
            return {
                'instances_cached': len(cls._cache),
                'instances_with_cached_settings': instances_with_cached_settings
            }
