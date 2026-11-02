"""The evaluation window constants of the stage-1 grid, in a module of their own (standard library only) so the CLI can
quote them in its help without importing the grid runner (pandas, the model libraries, ...)."""
from datetime import date

#: the out-of-sample evaluation of the exploration: first session scored, last session of the data
EVAL_START, EVAL_END = date(2019, 1, 1), date(2026, 10, 12)
#: the first day of the feature / bar history the training windows may reach back to
DATA_START = date(2016, 1, 4)
