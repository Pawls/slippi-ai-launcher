import numpy as np
from numpy.typing import NDArray

def voluntary_death_forward_fill(
    returned: NDArray[np.bool_],
    voluntary_departure: NDArray[np.bool_],
    knocked_departure: NDArray[np.bool_],
    deaths: NDArray[np.bool_],
) -> NDArray[np.bool_]: ...
