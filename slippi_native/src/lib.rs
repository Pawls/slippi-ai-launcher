use numpy::{PyArrayDyn, PyReadonlyArrayDyn};
use pyo3::prelude::*;

/// Forward-fill state machine for voluntary offstage death detection.
///
/// Replaces the Python loop in `voluntary_offstage_death` (reward.py).
/// Inputs are boolean arrays of shape (T-1,) or (T-1, num_envs).
/// Returns a boolean array of the same shape: True where the player
/// died during a voluntary offstage excursion.
#[pyfunction]
fn voluntary_death_forward_fill<'py>(
    py: Python<'py>,
    returned: PyReadonlyArrayDyn<'py, bool>,
    voluntary_departure: PyReadonlyArrayDyn<'py, bool>,
    knocked_departure: PyReadonlyArrayDyn<'py, bool>,
    deaths: PyReadonlyArrayDyn<'py, bool>,
) -> PyResult<Bound<'py, PyArrayDyn<bool>>> {
    let ret = returned.as_array();
    let vol = voluntary_departure.as_array();
    let knock = knocked_departure.as_array();
    let die = deaths.as_array();

    let shape = ret.shape();
    let ndim = shape.len();

    if ndim == 1 {
        let t = shape[0];
        let mut result = vec![false; t];
        let mut state: i8 = 0;

        for i in 0..t {
            if ret[i] {
                state = 0;
            }
            if vol[i] {
                state = 1;
            }
            if knock[i] {
                state = -1;
            }
            result[i] = die[i] && state == 1;
        }

        let arr = numpy::ndarray::ArrayD::from_shape_vec(
            numpy::ndarray::IxDyn(&[t]), result
        ).unwrap();
        Ok(PyArrayDyn::from_owned_array(py, arr))
    } else if ndim == 2 {
        let t = shape[0];
        let num_envs = shape[1];
        let mut result = vec![false; t * num_envs];
        let mut states = vec![0i8; num_envs];

        for i in 0..t {
            for j in 0..num_envs {
                let idx = [i, j];
                if ret[idx] {
                    states[j] = 0;
                }
                if vol[idx] {
                    states[j] = 1;
                }
                if knock[idx] {
                    states[j] = -1;
                }
                result[i * num_envs + j] = die[idx] && states[j] == 1;
            }
        }

        let arr = numpy::ndarray::ArrayD::from_shape_vec(
            numpy::ndarray::IxDyn(&[t, num_envs]), result
        ).unwrap();
        Ok(PyArrayDyn::from_owned_array(py, arr))
    } else {
        Err(pyo3::exceptions::PyValueError::new_err(
            format!("Expected 1D or 2D arrays, got {}D", ndim),
        ))
    }
}

/// Native accelerators for slippi-ai reward computation.
#[pymodule]
fn slippi_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(voluntary_death_forward_fill, m)?)?;
    Ok(())
}
