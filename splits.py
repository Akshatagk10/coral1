import numpy as np
def grouped_split(groups, test_frac=0.3, seed=0):
    """Split row indices so no group appears on both sides."""
    groups = np.asarray(groups); ug = np.unique(groups)
    np.random.default_rng(seed).shuffle(ug)
    test_g = set(ug[:max(1, round(len(ug) * test_frac))])
    m = np.array([g in test_g for g in groups])
    return np.where(~m)[0], np.where(m)[0]
