import h5py
import numpy as np

from mpi4py import MPI

from sklearn.linear_model import LinearRegression, Lasso
from sklearn.linear_model.base import (
    LinearModel, _preprocess_data, SparseCoefMixin)
from sklearn.linear_model.coordinate_descent import _alpha_grid
from sklearn.metrics import r2_score
from sklearn.model_selection import train_test_split
from sklearn.utils import check_X_y
import warnings
warnings.filterwarnings(action='ignore', category=FutureWarning)

from PyUoI import utils

_np2mpi = utils._np2mpi

def load_data_MPI(h5_name, X_key='X', y_key='y'):
    comm = MPI.COMM_WORLD
    rank = comm.rank
    with h5py.File(h5_name, 'r') as f:
        if rank == 0:
            X = f[X_key].value
            y = f[y_key].value
        else:
            X = np.empty(f[X_key].shape, dtype=f[X_key].dtype)
            y = np.empty(f[y_key].shape, dtype=f[y_key].dtype)
    comm.Bcast([X, _np2mpi[np.dtype(X.dtype)]], root=0)
    comm.Bcast([y, _np2mpi[np.dtype(y.dtype)]], root=0)
    return X, y


class UoI_Lasso(LinearModel, SparseCoefMixin):
    """The UoI-Lasso algorithm.

    See Bouchard et al., NIPS, 2017, for more details on UoI-Lasso and the
    Union of Intersections framework.

    Parameters
    ----------
    n_lambdas : int, default 48
        The number of L1 penalties to sweep across. For each lambda value,
        UoI-Lasso will fit that model over many bootstraps of the data. A
        larger set of L1 penalties will consider a more diverse set of supports
        while increasing compute time.

    n_boots_sel : int, default 48
        The number of data bootstraps to use in the selection module.
        Increasing this number will make selection more strict.

    n_boots_est : int, default 48
        The number of data bootstraps to use in the estimation module.
        Increasing this number will relax selection and decrease variance.

    selection_frac : float, default 0.9
        The fraction of the dataset to use for training in each resampled
        bootstrap, during the selection module. Small values of this parameter
        imply larger "perturbations" to the dataset.

    estimation_frac : float, default 0.9
        The fraction of the dataset to use for training in each resampled
        bootstrap, during the estimation module. The remaining data is used
        to obtain validation scores. Small values of this parameters imply
        larger "perturbations" to the dataset.

    stability_selection : int, float, or array-like, default 1
        If int, treated as the number of bootstraps that a feature must
        appear in to guarantee placement in selection profile. If float,
        must be between 0 and 1, and is instead the proportion of
        bootstraps. If array-like, must consist of either ints or floats
        between 0 and 1. In this case, each entry in the array-like object
        will act as a separate threshold for placement in the selection
        profile.

    copy_X : boolean, default True
        If True, X will be copied; else, it may be overwritten.

    fit_intercept : boolean, default True
        Whether to calculate the intercept for this model. If set
        to False, no intercept will be used in calculations
        (e.g. data is expected to be already centered).

    normalize : boolean, default False
        This parameter is ignored when ``fit_intercept`` is set to False.
        If True, the regressors X will be normalized before regression by
        subtracting the mean and dividing by the l2-norm.

    random_state : int, RandomState instance or None, default None
        The seed of the pseudo random number generator that selects a random
        feature to update.  If int, random_state is the seed used by the random
        number generator; If RandomState instance, random_state is the random
        number generator; If None, the random number generator is the
        RandomState instance used by `np.random`.

    Attributes
    ----------
    coef_ : array, shape (n_features,) or (n_targets, n_features)
        Estimated coefficients for the linear regression problem.

    intercept_ : float
        Independent term in the linear model.

    supports_ : array, shape
        boolean array indicating whether a given regressor (column) is selected
        for estimation for a given lambda (row).
    """

    def __init__(
        self,
        n_boots_sel=48, n_boots_est=48,
        selection_frac=0.9, estimation_frac=0.9,
        n_lambdas=48, stability_selection=1., eps=1e-3, warm_start=True,
        estimation_score='r2',
        copy_X=True, fit_intercept=True, normalize=True, seed=20181205
    ):
        # data split fractions
        self.selection_frac = selection_frac
        self.estimation_frac = estimation_frac
        # number of bootstraps
        self.n_boots_sel = n_boots_sel
        self.n_boots_est = n_boots_est
        # other hyperparameters
        self.n_lambdas = n_lambdas
        self.stability_selection = stability_selection
        self.eps = eps
        self.estimation_score = estimation_score
        self.warm_start = warm_start
        # preprocessing
        self.copy_X = copy_X
        self.fit_intercept = fit_intercept
        self.normalize = normalize
        self.comm = MPI.COMM_WORLD
        self.rank = self.comm.rank
        self.size = self.comm.size
        self.random_state = np.random.RandomState(seed + self.rank)
        self._seed = seed

    def fit(
        self, X, y, stratify=None, verbose=False
    ):
        """Fit data according to the UoI-Lasso algorithm.

        Parameters
        ----------
        X : ndarray or scipy.sparse matrix, (n_samples, n_features)
            The design matrix.

        y : ndarray, shape (n_samples,)
            Response vector. Will be cast to X's dtype if necessary.
            Currently, this implementation does not handle multiple response
            variables.

        stratify : array-like or None, default None
            Ensures groups of samples are alloted to training/test sets
            proportionally. Labels for each group must be an int greater
            than zero. Must be of size equal to the number of samples, with
            further restrictions on the number of groups.

        verbose : boolean
            A switch indicating whether the fitting should print out messages
            displaying progress. Utilizes tqdm to indicate progress on
            bootstraps.
        """
        # perform checks
        X, y = check_X_y(X, y, accept_sparse=['csr', 'csc', 'coo'],
                         y_numeric=True, multi_output=True)

        # preprocess data
        X, y, X_offset, y_offset, X_scale = _preprocess_data(
            X, y, fit_intercept=self.fit_intercept, normalize=self.normalize,
            copy=self.copy_X
        )

        # extract model dimensions
        self.n_samples, self.n_features = X.shape

        ####################
        # Selection Module #
        ####################
        # choose the lamba parameters for selection sweep
        self.lambdas = _alpha_grid(
            X=X, y=y,
            l1_ratio=1.0,
            fit_intercept=self.fit_intercept,
            eps=self.eps,
            n_alphas=self.n_lambdas,
            normalize=self.normalize
        )

        # initialize selection

        if self.size > self.n_boots_sel:
            my_boots = np.array_split(
                np.arange(self.n_boots_sel * self.n_lambdas), self.size
            )[self.rank]
            my_selection_coefs = np.zeros(
                (my_boots.size, self.n_features))

            # iterate over bootstrap samples
            for ii, my_selection_idx in enumerate(my_boots):
                our_seed = my_selection_idx // self.n_lambdas
                lambda_idx = my_selection_idx % self.n_lambdas

                rng = np.random.RandomState(self._seed + our_seed)
                # draw a resampled bootstrap
                X_train, X_test, y_train, y_test = train_test_split(
                    X, y,
                    train_size=self.selection_frac,
                    stratify=stratify,
                    random_state=rng
                )

                lamb = self.lambdas[lambda_idx]

                # perform a sweep over the regularization strengths
                my_selection_coefs[ii] = np.squeeze(self.lasso_sweep(
                    X=X_train, y=y_train,
                    lambdas=np.array([lamb]),
                    warm_start=self.warm_start,
                    random_state=self.random_state))

            selection_coefs = utils.Gatherv_rows(
                send=my_selection_coefs,
                comm=self.comm,
                root=0)
            if self.rank == 0:
                initial = selection_coefs[:self.n_lambdas]
                selection_coefs = selection_coefs.reshape(
                    (self.n_boots_sel, self.n_lambdas, self.n_features))
        else:
            # split up bootstraps into processes
            my_boots = np.array_split(
                np.arange(self.n_boots_sel), self.size
            )[self.rank]

            my_selection_coefs = np.zeros(
                (my_boots.size, self.n_lambdas, self.n_features)
            )

            # iterate over bootstraps
            for ii in range(my_boots.size):
                # draw a resampled bootstrap
                X_train, X_test, y_train, y_test = train_test_split(
                    X, y,
                    train_size=self.selection_frac,
                    stratify=stratify,
                    random_state=self.random_state
                )

                # perform a sweep over the regularization strengths
                my_selection_coefs[ii] = self.lasso_sweep(
                    X=X_train, y=y_train,
                    lambdas=self.lambdas,
                    warm_start=self.warm_start,
                    random_state=self.random_state
                )

            selection_coefs = utils.Gatherv_rows(
                send=my_selection_coefs,
                comm=self.comm,
                root=0)

        # perform the intersection step
        if self.rank == 0:
            self.intersection(selection_coefs)
            self.supports = self.supports.astype('int32')
            supports_shape = self.supports.shape
        else:
            self.supports = None
            supports_shape = None
        supports_shape = self.comm.bcast(supports_shape, root=0)
        if self.rank != 0:
            self.supports = np.empty(supports_shape, dtype=np.intc)

        self.comm.Bcast(
            [self.supports, _np2mpi[np.dtype(np.intc)]],
            root=0)

        self.comm.Barrier()
        self.supports = self.supports.astype(bool)
        #####################
        # Estimation Module #
        #####################
        # set up data arrays
        n_supports = self.supports.shape[0]
        my_estimate_idxs = np.array_split(
            np.arange(self.n_boots_est * n_supports), self.size
        )[self.rank]
        my_estimates = np.zeros((my_estimate_idxs.size, self.n_features))
        my_scores = np.zeros((my_estimate_idxs.size))
        # iterate over bootstrap samples
        for ii, my_estimate_idx in enumerate(my_estimate_idxs):
            our_seed = my_estimate_idx // n_supports
            support_idx = my_estimate_idx % n_supports
            support = self.supports[support_idx]

            rng = np.random.RandomState(self._seed - 1 - our_seed)
            # draw a resampled bootstrap
            X_train, X_test, y_train, y_test = train_test_split(
                X, y,
                train_size=self.estimation_frac,
                stratify=stratify,
                random_state=rng
            )

            if np.any(support):
                # compute ols estimate
                ols = LinearRegression()
                ols.fit(
                    X_train[:, support],
                    y_train
                )

                # store the fitted coefficients
                my_estimates[ii, support] = ols.coef_.ravel()

                # obtain predictions for scoring
                y_pred = ols.predict(X_test[:, support])
            else:
                # no prediction since nothing was selected
                y_pred = np.zeros(y_test.size)

            # only negate if we're using an information criterion
            negate = (
                self.estimation_score == 'BIC' or
                self.estimation_score == 'AIC' or
                self.estimation_score == 'AICc'
            )

            # calculate estimation score
            my_scores[ii] = self.score_predictions(
                y_true=y_test,
                y_pred=y_pred,
                n_features=np.count_nonzero(support),
                metric=self.estimation_score,
                negate=negate
            )

        estimation_coefs = utils.Gatherv_rows(
            send=my_estimates,
            comm=self.comm,
            root=0)

        self.scores = utils.Gatherv_rows(
            send=my_scores,
            comm=self.comm,
            root=0)

        if self.rank == 0:
            self.scores = self.scores.reshape(self.n_boots_est, n_supports)
            estimation_coefs = estimation_coefs.reshape(self.n_boots_est, n_supports, -1)
            self.lambda_max_idx = np.argmax(self.scores, axis=1)
            # extract the estimates over bootstraps from model with best lambda
            best_estimates = estimation_coefs[
                np.arange(self.n_boots_est), self.lambda_max_idx, :
            ]
            # take the median across estimates for the final, bagged estimate
            self.coef_ = np.median(best_estimates, axis=0)
        else:
            self.coef_ = np.empty(self.n_features, dtype=X.dtype)

        self.comm.Bcast([self.coef_, _np2mpi[np.dtype(X.dtype)]], root=0)
        self._set_intercept(X_offset, y_offset, X_scale)

        return self

    def stability_selection_to_threshold(self, stability_selection):
        """Converts user inputted stability selection to an array of
        thresholds. These thresholds correspond to the number of bootstraps
        that a feature must appear in to guarantee placement in the selection
        profile.

        Parameters
        ----------
        stability_selection : int, float, or array-like
            If int, treated as the number of bootstraps that a feature must
            appear in to guarantee placement in selection profile. If float,
            must be between 0 and 1, and is instead the proportion of
            bootstraps. If array-like, must consist of either ints or floats
            between 0 and 1. In this case, each entry in the array-like object
            will act as a separate threshold for placement in the selection
            profile.
        """

        # single float, indicating proportion of bootstraps
        if isinstance(stability_selection, float):
            selection_thresholds = np.array([int(
                stability_selection * self.n_boots_sel
            )])

        # single int, indicating number of bootstraps
        elif isinstance(stability_selection, int):
            selection_thresholds = np.array([int(
                stability_selection
            )])

        # list, to be converted into numpy array
        elif isinstance(stability_selection, list):
            # list of floats
            if all(isinstance(idx, float) for idx in stability_selection):
                selection_thresholds = \
                    self.n_boots_sel * np.array(stability_selection)

            # list of ints
            elif all(isinstance(idx, int) for idx in stability_selection):
                selection_thresholds = np.array(stability_selection)

            else:
                raise ValueError("Stability selection list must consist of "
                                 "floats or ints.")

        # numpy array
        elif isinstance(stability_selection, np.ndarray):
            # np array of floats
            if np.issubdtype(stability_selection.dtype.type, np.floating):
                selection_thresholds = self.n_boots_sel * stability_selection

            # np array of ints
            elif np.issubdtype(stability_selection.dtype.type, np.integer):
                selection_thresholds = stability_selection

            else:
                raise ValueError("Stability selection array must consist of "
                                 "floats or ints.")

        else:
            raise ValueError("Stability selection must be a valid float, int "
                             "or array.")

        # ensure that ensuing list of selection thresholds satisfies
        # the correct bounds
        selection_thresholds = selection_thresholds.astype('int')
        if not (
            np.all(selection_thresholds <= self.n_boots_sel) and
            np.all(selection_thresholds > 1)
        ):
            raise ValueError("Stability selection thresholds must be within "
                             "the correct bounds.")

        return selection_thresholds

    def intersection(self, coefs):
        """Performs the intersection operation on selection coefficients
        using stability selection criteria.

        Parameters
        ----------
        coefs : np.ndarray, shape (# bootstraps, # lambdas, # features)
            The coefficients obtain from the selection sweep, corresponding to
            each bootstrap and choice of L1 regularization strength.
        """

        # extract selection thresholds from user provided stability selection
        self.selection_thresholds = self.stability_selection_to_threshold(
            stability_selection=self.stability_selection
        )

        # create support matrix
        self.n_selection_thresholds = self.selection_thresholds.size
        self.supports = np.zeros(
            (self.n_selection_thresholds, self.n_lambdas, self.n_features),
            dtype=bool
        )

        # iterate over each stability selection threshold
        for thresh_idx, threshold in enumerate(self.selection_thresholds):
            # calculate the support given the specific selection threshold
            self.supports[thresh_idx, ...] = \
                np.count_nonzero(coefs, axis=0) >= threshold

        # unravel the dimension corresponding to selection thresholds
        self.supports = np.squeeze(np.reshape(
            self.supports,
            (self.n_selection_thresholds * self.n_lambdas, self.n_features)
        ))

        return

    def score_predictions(
        self, y_true, y_pred, metric='r2', negate=False, **kwargs
    ):
        """Score, according to some metric, predictions provided by a model.

        Parameters
        ----------
        y_true : array-like
            The true response variables.

        y_pred : array-like
            The predicted response variables.

        metric : string
            The type of score to run on the prediction. Valid options include
            'r2' (explained variance), 'BIC' (Bayesian information criterion),
            'AIC' (Akaike information criterion), and 'AICc' (corrected AIC).

        negate : bool
            Whether to negate the score. Useful in cases like AIC and BIC,
            where minimum score is preferable.

        Returns
        -------
        score : float
            The score.
        """

        if metric == 'r2':
            score = r2_score(
                y_true=y_true,
                y_pred=y_pred
            )
        elif self.estimation_score == 'BIC':
            score = utils.BIC(
                y_true=y_true,
                y_pred=y_pred,
                n_features=kwargs.get('n_features')
            )
        elif self.estimation_score == 'AIC':
            score = utils.AIC(
                y_true=y_true,
                y_pred=y_pred,
                n_features=kwargs.get('n_features')
            )
        elif self.estimation_score == 'AICc':
            score = utils.AICc(
                y_true=y_true,
                y_pred=y_pred,
                n_features=kwargs.get('n_features')
            )
        else:
            raise ValueError(
                metric + ' is not a valid option.'
            )

        # negate score
        if negate:
            score = -score

        return score

    @staticmethod
    def lasso_sweep(
        X, y, lambdas, normalize=True, max_iter=10000, warm_start=True,
        random_state=None
    ):
        """Perform Lasso regression on a dataset over a sweep
        of L1 penalty values.

        Parameters
        ----------
        X : ndarray or scipy.sparse matrix, (n_samples, n_features)
            The design matrix.

        y : ndarray, shape (n_samples,)
            Response vector.

        lambdas : ndarray, shape (n_lambdas)
            The set of regularization parameters over which to run lasso fits.

        normalize : boolean, default False
            If True, the regressors X will be normalized before regression by
            subtracting the mean and dividing by the l2-norm.

        max_iter : int, default 10000
            The maximum number of iterations.

        warm_start : bool, optional
            When set to True, reuse the solution of the previous call to fit as
            initialization, otherwise, just erase the previous solution.

        random_state : int, RandomState instance or None, default None
            The seed of the pseudo random number generator that selects a
            random feature to update.  If int, random_state is the seed used by
            the random number generator; If RandomState instance, random_state
            is the random number generator; If None, the random number
            generator is the RandomState instance used by `np.random`.

        Returns
        -------
        coefs : nd.array, shape (n_lambdas, n_features)
            Predicted parameter values for each regularization strength.
        """

        n_lambdas = len(lambdas)
        n_features = X.shape[1]

        coefs = np.zeros(
            (n_lambdas, n_features),
            dtype=np.float32
        )

        # initialize lasso fit object
        lasso = Lasso(
            normalize=normalize,
            max_iter=max_iter,
            warm_start=warm_start,
            random_state=random_state
        )

        # apply the Lasso to bootstrapped datasets
        for lamb_idx, lamb in enumerate(lambdas):
            # reset the regularization parameter
            lasso.set_params(alpha=lamb)
            # rerun fit
            lasso.fit(X, y)
            # store coefficients
            coefs[lamb_idx, :] = lasso.coef_

        return coefs