import { useState, useEffect, useRef, useCallback } from 'react';

export interface SystemResources {
  cpuPercent?: number;
  memoryUsedMB?: number;
  memoryTotalMB?: number;
  memoryPercent?: number;
  gpuUtilization?: number | null;
  gpuMemoryUsedMB?: number | null;
  gpuMemoryTotalMB?: number | null;
}

export interface EpochMetric {
  epoch: number;
  train_loss?: number;
  val_loss?: number;
  accuracy?: number;
  val_accuracy?: number;
  [key: string]: number | undefined;
}

export interface JobProgressData {
  type: 'connected' | 'progress' | 'log' | 'complete' | 'error';
  job_id: string;
  status?: string;
  progress?: number;
  // Generation/Individual progress
  currentGeneration?: number;
  totalGenerations?: number;
  currentIndividual?: number;
  populationSize?: number;
  // Epoch/Training progress
  currentEpoch?: number;
  totalEpochs?: number;
  currentModelType?: string;
  currentModelParams?: Record<string, number | string>;
  // Metrics
  currentLoss?: number;
  currentAccuracy?: number;
  bestFitness?: number;
  // Error tracking
  errorCount?: number;
  successCount?: number;
  // Individuals count
  individualsCount?: number;
  // System resources
  systemResources?: SystemResources;
  gpuUtilization?: number;
  estimatedTimeRemaining?: string;
  // Dataset info
  datasetProgress?: { current: number; total: number };
  currentDatasetId?: number;
  // Epoch history (for live chart)
  epochHistory?: EpochMetric[];
  // Log/error messages
  message?: string;
  timestamp?: string;
}

export interface UseJobWebSocketOptions {
  onProgress?: (data: JobProgressData) => void;
  onLog?: (message: string) => void;
  onComplete?: (data: JobProgressData) => void;
  onError?: (error: string) => void;
  reconnectAttempts?: number;
  reconnectInterval?: number;
  enabled?: boolean;
}

export interface UseJobWebSocketReturn {
  isConnected: boolean;
  lastMessage: JobProgressData | null;
  error: string | null;
  reconnect: () => void;
}

export function useJobWebSocket(
  jobId: string | null,
  options: UseJobWebSocketOptions = {}
): UseJobWebSocketReturn {
  const {
    onProgress,
    onLog,
    onComplete,
    onError,
    reconnectAttempts = 5,
    reconnectInterval = 2000,
    enabled = true,
  } = options;

  const [isConnected, setIsConnected] = useState(false);
  const [lastMessage, setLastMessage] = useState<JobProgressData | null>(null);
  const [error, setError] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectCountRef = useRef(0);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pingIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const cleanup = useCallback(() => {
    if (pingIntervalRef.current) {
      clearInterval(pingIntervalRef.current);
      pingIntervalRef.current = null;
    }
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
  }, []);

  const connect = useCallback(() => {
    if (!jobId || !enabled) return;

    cleanup();

    // Determine WebSocket URL based on current location
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.hostname;
    // Use port 8000 for API (backend) in development
    const port = window.location.port === '5173' ? '8000' : window.location.port;
    const wsUrl = `${protocol}//${host}:${port}/api/ws/jobs/${jobId}`;

    try {
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        setIsConnected(true);
        setError(null);
        reconnectCountRef.current = 0;

        // Set up ping interval for keep-alive
        pingIntervalRef.current = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send('ping');
          }
        }, 30000);
      };

      ws.onmessage = (event) => {
        try {
          // Handle pong response
          if (event.data === 'pong') return;

          const data: JobProgressData = JSON.parse(event.data);
          setLastMessage(data);

          switch (data.type) {
            case 'connected':
              // Connection confirmed
              break;
            case 'progress':
              onProgress?.(data);
              break;
            case 'log':
              onLog?.(data.message || '');
              break;
            case 'complete':
              onComplete?.(data);
              break;
            case 'error':
              setError(data.message || 'Unknown error');
              onError?.(data.message || 'Unknown error');
              break;
          }
        } catch (e) {
          console.error('Failed to parse WebSocket message:', e);
        }
      };

      ws.onerror = (event) => {
        console.error('WebSocket error:', event);
        setError('WebSocket connection error');
      };

      ws.onclose = (event) => {
        setIsConnected(false);

        // Don't reconnect if job is complete or was closed intentionally
        if (event.code === 1000) return;

        // Attempt reconnection
        if (reconnectCountRef.current < reconnectAttempts) {
          reconnectCountRef.current++;
          reconnectTimeoutRef.current = setTimeout(() => {
            connect();
          }, reconnectInterval);
        } else {
          setError('Failed to connect after multiple attempts');
          onError?.('Connection lost. Please refresh the page.');
        }
      };
    } catch (e) {
      console.error('Failed to create WebSocket:', e);
      setError('Failed to create WebSocket connection');
    }
  }, [jobId, enabled, cleanup, onProgress, onLog, onComplete, onError, reconnectAttempts, reconnectInterval]);

  const reconnect = useCallback(() => {
    reconnectCountRef.current = 0;
    connect();
  }, [connect]);

  useEffect(() => {
    connect();
    return cleanup;
  }, [connect, cleanup]);

  return {
    isConnected,
    lastMessage,
    error,
    reconnect,
  };
}

export default useJobWebSocket;
