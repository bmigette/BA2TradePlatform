import React, { useEffect } from 'react';
import { X, CheckCircle, AlertCircle, Info, AlertTriangle } from 'lucide-react';

interface ToastProps {
  message: string;
  type?: 'success' | 'error' | 'info' | 'warning';
  duration?: number;
  onClose: () => void;
}

const Toast: React.FC<ToastProps> = ({ message, type = 'info', duration = 3000, onClose }) => {
  useEffect(() => {
    const timer = setTimeout(() => {
      onClose();
    }, duration);

    return () => clearTimeout(timer);
  }, [duration, onClose]);

  const typeStyles = {
    success: {
      bg: 'bg-green-50 dark:bg-green-900',
      border: 'border-green-500',
      text: 'text-green-800 dark:text-green-200',
      icon: <CheckCircle className="text-green-500" size={20} />,
    },
    error: {
      bg: 'bg-red-50 dark:bg-red-900',
      border: 'border-red-500',
      text: 'text-red-800 dark:text-red-200',
      icon: <AlertCircle className="text-red-500" size={20} />,
    },
    warning: {
      bg: 'bg-yellow-50 dark:bg-yellow-900',
      border: 'border-yellow-500',
      text: 'text-yellow-800 dark:text-yellow-200',
      icon: <AlertTriangle className="text-yellow-500" size={20} />,
    },
    info: {
      bg: 'bg-blue-50 dark:bg-blue-900',
      border: 'border-blue-500',
      text: 'text-blue-800 dark:text-blue-200',
      icon: <Info className="text-blue-500" size={20} />,
    },
  };

  const styles = typeStyles[type];

  return (
    <div className="fixed top-4 right-4 z-50 animate-slide-in">
      <div className={`${styles.bg} ${styles.border} border-l-4 p-4 rounded-md shadow-lg max-w-md`}>
        <div className="flex items-start">
          <div className="flex-shrink-0">
            {styles.icon}
          </div>
          <div className={`ml-3 flex-1 ${styles.text}`}>
            <p className="text-sm font-medium">{message}</p>
          </div>
          <button
            onClick={onClose}
            className={`ml-3 flex-shrink-0 ${styles.text} hover:opacity-70`}
          >
            <X size={16} />
          </button>
        </div>
      </div>
    </div>
  );
};

export default Toast;
