#include "k_fixed_point.cuh"
#include "k_lambda_transformer.cuh"
#include "surreal.cuh"

void __global__ k_compute_w_coords(
    const int N,
    const double lambda,
    const double cutoff,
    const int *__restrict__ lambda_plane_idxs, // 0 or 1, shift
    const int *__restrict__ lambda_offset_idxs,
    double *__restrict__ coords_w,
    double *__restrict__ dw_dl) {

    int atom_i_idx = blockIdx.x * blockDim.x + threadIdx.x;

    if (atom_i_idx >= N) {
        return;
    }

    int lambda_offset_i = atom_i_idx < N ? lambda_offset_idxs[atom_i_idx] : 0;
    int lambda_plane_i = atom_i_idx < N ? lambda_plane_idxs[atom_i_idx] : 0;

    double f_lambda = transform_lambda_w(lambda);

    double step = 1e-7;
    Surreal<double> lambda_surreal(lambda, step);
    double f_lambda_grad = (transform_lambda_w(lambda_surreal).imag) / step;

    double coords_w_i = (lambda_plane_i + lambda_offset_i * f_lambda) * cutoff;
    double dw_dl_i = lambda_offset_i * f_lambda_grad * cutoff;

    coords_w[atom_i_idx] = coords_w_i;
    dw_dl[atom_i_idx] = dw_dl_i;

} // 0 or 1, how much we offset from the plane by )

void __global__ k_gather_interpolated(
    const double lambda,
    const int N,
    const unsigned int *__restrict__ idxs,
    const double *__restrict__ d_p0, // [P] initial parameters
    const double *__restrict__ d_p1, // [P] final parameters
    double *__restrict__ d_gathered_p,
    double *__restrict__ d_gathered_dp_dl) {

    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    int stride = gridDim.y;
    int stride_idx = blockIdx.y;

    if (idx >= N) {
        return;
    }

    int source_idx = idx * stride + stride_idx;
    int target_idx = idxs[idx] * stride + stride_idx;

    double step = 1e-7;
    Surreal<double> lambda_surreal(lambda, step);

    double f_lambda;
    double f_lambda_grad;

    if (stride_idx == 0) {
        f_lambda = transform_lambda_charge(lambda);
        f_lambda_grad = (transform_lambda_charge(lambda_surreal).imag) / step;
    }
    if (stride_idx == 1) {
        f_lambda = transform_lambda_sigma(lambda);
        f_lambda_grad = (transform_lambda_sigma(lambda_surreal).imag) / step;
    }
    if (stride_idx == 2) {
        f_lambda = transform_lambda_epsilon(lambda);
        f_lambda_grad = (transform_lambda_epsilon(lambda_surreal).imag) / step;
    }

    d_gathered_p[source_idx] = (1 - f_lambda) * d_p0[target_idx] + f_lambda * d_p1[target_idx];
    d_gathered_dp_dl[source_idx] = f_lambda_grad * (d_p1[target_idx] - d_p0[target_idx]);
}

void __global__ k_add_du_dp_interpolated(
    const double lambda,
    const int N,
    const unsigned long long *__restrict__ src,
    unsigned long long *__restrict__ dest) {

    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    int stride = gridDim.y;
    int stride_idx = blockIdx.y;

    if (idx >= N) {
        return;
    }

    int size = N * stride;
    int target_idx = idx * stride + stride_idx;

    // handle charges, sigmas, epsilons with different exponents
    if (stride_idx == 0) {
        double f_lambda = transform_lambda_charge(lambda);
        dest[target_idx] += FLOAT_TO_FIXED_DU_DP<double, FIXED_EXPONENT_DU_DCHARGE>(
            (1 - f_lambda) * FIXED_TO_FLOAT_DU_DP<double, FIXED_EXPONENT_DU_DCHARGE>(src[target_idx]));
        dest[size + target_idx] += FLOAT_TO_FIXED_DU_DP<double, FIXED_EXPONENT_DU_DCHARGE>(
            f_lambda * FIXED_TO_FLOAT_DU_DP<double, FIXED_EXPONENT_DU_DCHARGE>(src[target_idx]));
    } else if (stride_idx == 1) {
        double f_lambda = transform_lambda_sigma(lambda);
        dest[target_idx] += FLOAT_TO_FIXED_DU_DP<double, FIXED_EXPONENT_DU_DSIG>(
            (1 - f_lambda) * FIXED_TO_FLOAT_DU_DP<double, FIXED_EXPONENT_DU_DSIG>(src[target_idx]));
        dest[size + target_idx] += FLOAT_TO_FIXED_DU_DP<double, FIXED_EXPONENT_DU_DSIG>(
            f_lambda * FIXED_TO_FLOAT_DU_DP<double, FIXED_EXPONENT_DU_DSIG>(src[target_idx]));
    } else if (stride_idx == 2) {
        double f_lambda = transform_lambda_epsilon(lambda);
        dest[target_idx] += FLOAT_TO_FIXED_DU_DP<double, FIXED_EXPONENT_DU_DEPS>(
            (1 - f_lambda) * FIXED_TO_FLOAT_DU_DP<double, FIXED_EXPONENT_DU_DEPS>(src[target_idx]));
        dest[size + target_idx] += FLOAT_TO_FIXED_DU_DP<double, FIXED_EXPONENT_DU_DEPS>(
            f_lambda * FIXED_TO_FLOAT_DU_DP<double, FIXED_EXPONENT_DU_DEPS>(src[target_idx]));
    }
}