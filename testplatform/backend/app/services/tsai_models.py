"""
tsai Model Service for classification models.

Provides time series classification models using the tsai library.
Supports: LSTM, GRU, TCN, InceptionTime, ResNet, XceptionTime,
OmniScaleCNN, MiniRocket, PatchTST, LSTM-FCN, TST.
"""

# CRITICAL: Set matplotlib backend before any tsai/fastai imports
# tsai/fastai use matplotlib internally, and the default TkAgg backend
# causes errors when running in a web server (non-main thread)
import matplotlib
matplotlib.use('Agg')  # Use non-GUI backend

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Check for tsai availability
TSAI_AVAILABLE = False
CUDA_AVAILABLE = False
MPS_AVAILABLE = False
DEVICE = None
GPU_NAME = None

try:
    import torch
    CUDA_AVAILABLE = torch.cuda.is_available()
    MPS_AVAILABLE = hasattr(torch.backends, 'mps') and torch.backends.mps.is_available()

    if CUDA_AVAILABLE:
        DEVICE = torch.device('cuda')
        GPU_NAME = torch.cuda.get_device_name(0)
    elif MPS_AVAILABLE:
        DEVICE = torch.device('mps')
        GPU_NAME = 'Apple Silicon'
    else:
        DEVICE = torch.device('cpu')
        GPU_NAME = None

    from tsai.all import (
        LSTM, GRU, TCN, InceptionTime, ResNet, XceptionTime,
        OmniScaleCNN, MiniRocket, PatchTST, LSTM_FCN, TST
    )
    TSAI_AVAILABLE = True
    logger.info(f"tsai available. Device: {DEVICE}, GPU: {GPU_NAME}")
except ImportError as e:
    logger.warning(f"tsai not available: {e}")

from app.services.model_interface import IModelService


# PatchTST is designed for forecasting - wrap it for classification
class PatchTSTClassifier:
    """Wrapper to use PatchTST for classification tasks."""

    def __new__(cls, c_in, c_out, seq_len, **kwargs):
        """Create a PatchTST model with classification head."""
        import torch.nn as nn

        class _PatchTSTClassifier(nn.Module):
            def __init__(self, c_in, c_out, seq_len, **kwargs):
                super().__init__()
                # PatchTST outputs (batch, c_in, seq_len) for forecasting
                # We need (batch, c_out) for classification
                self.backbone = PatchTST(c_in=c_in, c_out=c_in, seq_len=seq_len, **kwargs)
                self.pool = nn.AdaptiveAvgPool1d(1)
                self.head = nn.Linear(c_in, c_out)

            def forward(self, x):
                out = self.backbone(x)  # (batch, c_in, seq_len)
                out = self.pool(out).squeeze(-1)  # (batch, c_in)
                out = self.head(out)  # (batch, c_out)
                return out

        return _PatchTSTClassifier(c_in, c_out, seq_len, **kwargs)


# Model class registry - maps model type to class (no eval)
MODEL_CLASSES = {}
if TSAI_AVAILABLE:
    MODEL_CLASSES = {
        'lstm': LSTM,
        'gru': GRU,
        'tcn': TCN,
        'inception': InceptionTime,
        'resnet': ResNet,
        'xception': XceptionTime,
        'omniscale': OmniScaleCNN,
        'minirocket': MiniRocket,
        'patchtst': PatchTSTClassifier,  # Use classifier wrapper
        'lstm_fcn': LSTM_FCN,
        'tst': TST,
    }


class TSAIModelService(IModelService):
    """
    tsai-based model service for time series classification.
    """

    MODEL_ARCHITECTURES = {
        'lstm': {
            'name': 'LSTM',
            'description': 'Long Short-Term Memory network',
            'default_params': {
                'hidden_size': 64,
                'n_layers': 2,
                'bidirectional': False,
                'dropout': 0.1,
            },
            'param_ranges': {
                'hidden_size': [32, 64, 128, 256],
                'n_layers': [1, 2, 3],
                'bidirectional': [True, False],
                'dropout': [0.0, 0.1, 0.2, 0.3],
            }
        },
        'gru': {
            'name': 'GRU',
            'description': 'Gated Recurrent Unit network',
            'default_params': {
                'hidden_size': 64,
                'n_layers': 2,
                'bidirectional': False,
                'dropout': 0.1,
            },
            'param_ranges': {
                'hidden_size': [32, 64, 128, 256],
                'n_layers': [1, 2, 3],
                'bidirectional': [True, False],
                'dropout': [0.0, 0.1, 0.2, 0.3],
            }
        },
        'tcn': {
            'name': 'TCN',
            'description': 'Temporal Convolutional Network',
            'default_params': {
                'layers': [64, 64, 64],
                'ks': 7,
                'conv_dropout': 0.1,
            },
            'param_ranges': {
                'layers': [[32, 32], [64, 64], [64, 64, 64], [128, 128]],
                'ks': [3, 5, 7, 9],
                'conv_dropout': [0.0, 0.1, 0.2],
            }
        },
        'inception': {
            'name': 'InceptionTime',
            'description': 'State-of-the-art CNN for time series (Fawaz 2019)',
            'default_params': {
                'nf': 32,
                'depth': 6,
                'ks': 40,
            },
            'param_ranges': {
                'nf': [16, 32, 64, 128],
                'depth': [3, 6, 9],
                'ks': [20, 40, 60],
            }
        },
        'resnet': {
            'name': 'ResNet',
            'description': 'Residual Network for time series',
            'default_params': {
                'nf': 64,
            },
            'param_ranges': {
                'nf': [32, 64, 128],
            }
        },
        'xception': {
            'name': 'XceptionTime',
            'description': 'Improved InceptionTime (Rahimian 2019)',
            'default_params': {
                'nf': 16,
            },
            'param_ranges': {
                'nf': [8, 16, 32, 64],
            }
        },
        'omniscale': {
            'name': 'OmniScaleCNN',
            'description': 'Multi-scale 1D-CNN (Tang 2020)',
            'default_params': {},
            'param_ranges': {}
        },
        'minirocket': {
            'name': 'MiniRocket',
            'description': 'Extremely fast, minimal training (Dempster 2021)',
            'default_params': {
                'num_features': 10000,
            },
            'param_ranges': {
                'num_features': [5000, 10000, 20000],
            }
        },
        'patchtst': {
            'name': 'PatchTST',
            'description': 'Long-horizon forecasting transformer (Nie 2022). NOT for classification.',
            'default_params': {
                'd_model': 128,
                'n_heads': 8,
                'patch_len': 16,
                'd_ff': 256,
                'dropout': 0.1,
                'activation': 'gelu',
            },
            'param_ranges': {
                'd_model': [64, 128, 256],
                'n_heads': [4, 8, 16],
                'patch_len': [8, 16, 24],
                'd_ff': [128, 256, 512],
                'dropout': [0.0, 0.1, 0.2],
                'activation': ['gelu', 'relu'],
            },
            'forecasting_only': True,  # Not suitable for classification
        },
        'lstm_fcn': {
            'name': 'LSTM-FCN',
            'description': 'Hybrid LSTM + CNN (Karim 2017)',
            'default_params': {
                'hidden_size': 128,
                'rnn_layers': 1,
                'cell_dropout': 0.1,
                'fc_dropout': 0.1,
            },
            'param_ranges': {
                'hidden_size': [64, 128, 256],
                'rnn_layers': [1, 2],
                'cell_dropout': [0.0, 0.1, 0.2],
                'fc_dropout': [0.0, 0.1, 0.2],
            }
        },
        'tst': {
            'name': 'TST',
            'description': 'Time Series Transformer (Zerveas 2020)',
            'default_params': {
                'd_model': 128,
                'n_heads': 8,
                'd_ff': 256,
                'n_layers': 3,
                'dropout': 0.1,
                'act': 'gelu',
            },
            'param_ranges': {
                'd_model': [64, 128, 256],
                'n_heads': [4, 8],
                'd_ff': [128, 256, 512],
                'n_layers': [2, 3, 4],
                'dropout': [0.0, 0.1, 0.2],
                'act': ['gelu', 'relu'],
            }
        },
    }

    def __init__(self, use_gpu: bool = True):
        """Initialize TSAIModelService."""
        self.use_gpu = use_gpu and (CUDA_AVAILABLE or MPS_AVAILABLE)
        if TSAI_AVAILABLE:
            import torch
            self.device = DEVICE if self.use_gpu else torch.device('cpu')
        else:
            self.device = None

        if self.use_gpu:
            logger.info(f"Using GPU: {GPU_NAME}")
        else:
            logger.info("Using CPU for model training")

    def get_available_models(self, include_forecasting: bool = False) -> Dict[str, Dict]:
        """Get available tsai model architectures for classification.

        Args:
            include_forecasting: If True, include forecasting-only models (default False)

        Returns:
            Dictionary of model architectures suitable for classification
        """
        return {k: {
            'name': v['name'],
            'description': v['description'],
            'default_params': v['default_params'],
        } for k, v in self.MODEL_ARCHITECTURES.items()
          if include_forecasting or not v.get('forecasting_only', False)}

    def get_parameter_ranges(self, model_type: str) -> Dict[str, List]:
        """Get hyperparameter ranges for a model type."""
        model_type = model_type.lower()
        if model_type not in self.MODEL_ARCHITECTURES:
            raise ValueError(f"Unknown model type: {model_type}")
        return self.MODEL_ARCHITECTURES[model_type]['param_ranges']

    def apply_layer_size_factor(self, params: Dict, factor: float) -> Dict:
        """Scale layer size parameters by a factor."""
        scaled = params.copy()
        size_params = ['hidden_size', 'nf', 'd_model', 'd_ff', 'num_features']

        for key in size_params:
            if key in scaled:
                scaled[key] = int(scaled[key] * factor)

        # Handle list params like 'layers'
        if 'layers' in scaled and isinstance(scaled['layers'], list):
            scaled['layers'] = [int(v * factor) for v in scaled['layers']]

        return scaled

    def create_model(
        self,
        model_type: str,
        params: Dict[str, Any],
        loss_fn: Any = None,
        epoch_callback: callable = None,
        c_in: int = None,
        c_out: int = 2,
        seq_len: int = None
    ) -> Any:
        """
        Create a tsai model architecture.

        Args:
            model_type: Model architecture name
            params: Model hyperparameters
            loss_fn: Optional custom loss function (used by Learner, not model)
            epoch_callback: Optional callback for epoch progress
            c_in: Number of input channels/features
            c_out: Number of output classes (default 2 for binary)
            seq_len: Sequence length

        Returns:
            Configured tsai model architecture (nn.Module)
        """
        if not TSAI_AVAILABLE:
            raise RuntimeError("tsai library not available")

        model_type = model_type.lower()
        if model_type not in self.MODEL_ARCHITECTURES:
            raise ValueError(f"Unknown model type: {model_type}. "
                           f"Supported: {list(self.MODEL_ARCHITECTURES.keys())}")

        arch_config = self.MODEL_ARCHITECTURES[model_type]
        p = {**arch_config['default_params'], **params}

        # Get model class from registry (no eval)
        model_class = MODEL_CLASSES.get(model_type)
        if model_class is None:
            raise ValueError(f"Model class not found for: {model_type}")

        # Build model based on type
        if model_type in ('lstm', 'gru'):
            model = model_class(
                c_in=c_in, c_out=c_out,
                hidden_size=p['hidden_size'],
                n_layers=p['n_layers'],
                bidirectional=p.get('bidirectional', False),
                rnn_dropout=p.get('dropout', 0.1),
                fc_dropout=p.get('dropout', 0.1),
            )
        elif model_type == 'tcn':
            model = model_class(
                c_in=c_in, c_out=c_out,
                layers=p.get('layers', [64, 64]),
                ks=p.get('ks', 7),
                conv_dropout=p.get('conv_dropout', 0.1),
            )
        elif model_type == 'inception':
            model = model_class(
                c_in=c_in, c_out=c_out,
                nf=p.get('nf', 32),
                depth=p.get('depth', 6),
                ks=p.get('ks', 40),
            )
        elif model_type == 'resnet':
            # ResNet only takes c_in and c_out
            model = model_class(
                c_in=c_in, c_out=c_out,
            )
        elif model_type == 'xception':
            model = model_class(
                c_in=c_in, c_out=c_out,
                nf=p.get('nf', 16),
            )
        elif model_type == 'omniscale':
            # OmniScaleCNN requires seq_len
            model = model_class(
                c_in=c_in, c_out=c_out, seq_len=seq_len,
            )
        elif model_type == 'minirocket':
            # MiniRocket - use the base model directly
            model = model_class(
                c_in=c_in, c_out=c_out, seq_len=seq_len,
            )
        elif model_type == 'patchtst':
            # PatchTST for classification - ensure patch_len divides evenly into seq_len
            patch_len = p.get('patch_len', 8)
            # Adjust patch_len to divide seq_len evenly
            while seq_len % patch_len != 0 and patch_len > 1:
                patch_len -= 1
            # Use smaller defaults for faster training and better stability
            model = model_class(
                c_in=c_in, c_out=c_out, seq_len=seq_len,
                d_model=p.get('d_model', 64),
                n_heads=p.get('n_heads', 4),
                patch_len=patch_len,
                stride=patch_len,  # Non-overlapping patches
                d_ff=p.get('d_ff', 128),
                dropout=p.get('dropout', 0.1),
            )
        elif model_type == 'lstm_fcn':
            model = model_class(
                c_in=c_in, c_out=c_out, seq_len=seq_len,
                hidden_size=p.get('hidden_size', 128),
                rnn_layers=p.get('rnn_layers', 1),
                cell_dropout=p.get('cell_dropout', 0.1),
                fc_dropout=p.get('fc_dropout', 0.1),
            )
        elif model_type == 'tst':
            model = model_class(
                c_in=c_in, c_out=c_out, seq_len=seq_len,
                d_model=p.get('d_model', 128),
                n_heads=p.get('n_heads', 8),
                d_ff=p.get('d_ff', 256),
                n_layers=p.get('n_layers', 3),
                dropout=p.get('dropout', 0.1),
                act=p.get('act', 'gelu'),
            )
        else:
            raise ValueError(f"Model creation not implemented for: {model_type}")

        logger.info(f"Created {model_type.upper()} model with params: {p}")
        return model

    @staticmethod
    def get_system_info() -> Dict[str, Any]:
        """Get system information for ML training."""
        return {
            'tsai_available': TSAI_AVAILABLE,
            'cuda_available': CUDA_AVAILABLE,
            'mps_available': MPS_AVAILABLE,
            'gpu_name': GPU_NAME,
            'device': str(DEVICE) if DEVICE else 'cpu',
        }
