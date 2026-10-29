"""UQ-DAAAM semantic uncertainty (arXiv:2606.08277, Sec. 3-4): log-volume of probability-weighted caption embeddings.

u = log det(Z^T Z + lam I), Z = [gamma_i r_i], r_i unit caption embeddings, gamma_i = exp(alpha (1 - p_i)).
Only numpy; no clustering, entropy or NLI model.
"""
import math

import numpy as np


def normalize(embeddings):
    embeddings = np.atleast_2d(np.asarray(embeddings, dtype=np.float64))
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    if not np.all(np.isfinite(embeddings)) or np.any(norms == 0):
        raise ValueError('caption embeddings must be finite and non-zero')
    return embeddings / norms


def gamma(probability, alpha=1.0):
    probability = float(probability)
    if not 0.0 <= probability <= 1.0:
        raise ValueError('token probability must be in [0, 1], got ' + str(probability))
    return math.exp(alpha * (1.0 - probability))


def weighted(embeddings, probabilities, alpha=1.0):
    """Z_j as a (d, r) matrix of gamma-weighted unit embeddings."""
    unit = normalize(embeddings)
    if len(probabilities) != len(unit):
        raise ValueError('one token probability per caption is required')
    return (unit * np.array([gamma(p, alpha) for p in probabilities])[:, None]).T


def log_volume(embeddings, probabilities, alpha=1.0, lam=1e-8):
    """Direct u_j = log det S_j via slogdet (reference for the incremental form)."""
    z = weighted(embeddings, probabilities, alpha)
    sign, value = np.linalg.slogdet(z.T @ z + lam * np.eye(z.shape[1]))
    if sign <= 0:
        raise ArithmeticError('S_j is not positive definite')
    return float(value)


class Uncertainty:
    """Incremental u_j (Thm 4.4): u+ - u = log s, s = gamma^2 + lam - eta, eta = b^T S^-1 b, b = Z^T z.

    eta and s are evaluated in the SVD basis of Z, where s = lam + |z_perp|^2 + sum c_k^2 lam / (sigma_k^2 + lam) is a sum of
    non-negative terms; the textbook subtraction loses all precision for near-duplicate captions when lam = 1e-8.
    """

    def __init__(self, alpha=1.0, lam=1e-8):
        if lam <= 0:
            raise ValueError('lambda must be positive')
        self.alpha, self.lam = float(alpha), float(lam)
        self.columns = []
        self.value = None

    def step(self, embedding, probability):
        """Effect of adding one caption without adding it."""
        g = gamma(probability, self.alpha)
        z = g * normalize(embedding)[0]
        if not self.columns:
            eta, s = 0.0, g * g + self.lam
        else:
            u, sigma, _ = np.linalg.svd(np.stack(self.columns, axis=1), full_matrices=False)
            c = u.T @ z
            residual = z - u @ c
            sigma2 = sigma * sigma
            eta = float(np.sum(sigma2 * c * c / (sigma2 + self.lam)))
            s = float(self.lam + residual @ residual + np.sum(c * c * self.lam / (sigma2 + self.lam)))
        delta = math.log(s)
        return {'gamma': g, 'eta': eta, 's': s, 'delta': delta,
                # Thm 4.5 criterion; equivalent to s < 1 since |r| = 1
                'decreases': bool(self.columns) and eta > g * g + self.lam - 1.0,
                'u_before': self.value, 'u_after': delta if self.value is None else self.value + delta, '_z': z}

    def add(self, embedding, probability):
        result = self.step(embedding, probability)
        self.columns.append(result.pop('_z'))
        self.value = result['u_after']
        return result
