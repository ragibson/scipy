import warnings

import numpy as np

from scipy import optimize
from scipy import integrate
from scipy import interpolate
import scipy.special as sc
from scipy._lib._util import _lazywhere
from ._distn_infrastructure import rv_continuous
from ._continuous_distns import uniform, expon, _norm_pdf
from ._fft_char_fn import pdf_from_cf_with_fft

# default numerical integration tolerance
# used for epsrel in piecewise and both epsrel and epsabs in dni
# (epsabs needed in dni since weighted quad requires epsabs > 0)
_QUAD_EPS = 1.2e-14


def Phi(alpha, t):
    return (
        np.tan(np.pi * alpha / 2)
        if alpha != 1
        else -2.0 * np.log(np.abs(t)) / np.pi
    )


def _cf(t, alpha, beta):
    """Characteristic function."""
    return np.exp(
        -(np.abs(t) ** alpha) * (1 - 1j * beta * np.sign(t) * Phi(alpha, t))
    )


def _pdf_single_value_cf_integrate(x, alpha, beta, **kwds):
    """To improve DNI accuracy convert characteristic function in to real
    valued integral using Euler's formula, then exploit cosine symmetry to
    change limits to [0, inf). Finally use cosine addition formula to split
    into two parts that can be handled by weighted quad pack.
    """
    quad_eps = kwds.get("quad_eps", _QUAD_EPS)

    def integrand1(t):
        if t == 0:
            return 0
        return np.exp(-(t ** alpha)) * (
            np.cos(beta * (t ** alpha) * Phi(alpha, t))
        )

    def integrand2(t):
        if t == 0:
            return 0
        return np.exp(-(t ** alpha)) * (
            np.sin(beta * (t ** alpha) * Phi(alpha, t))
        )

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", category=integrate.IntegrationWarning
        )
        warnings.filterwarnings("ignore", category=np.ComplexWarning)
        warnings.filterwarnings(
            "ignore", message="invalid value encountered in double_scalars"
        )
        int1, err1 = integrate.quad(
            integrand1,
            0,
            np.inf,
            weight="cos",
            wvar=x,
            limit=1000,
            epsabs=quad_eps,
            epsrel=quad_eps
        )

        int2, err2 = integrate.quad(
            integrand2,
            0,
            np.inf,
            weight="sin",
            wvar=x,
            limit=1000,
            epsabs=quad_eps,
            epsrel=quad_eps
        )

    return (int1 + int2) / np.pi


def _nolan_round_difficult_input(x0, alpha, beta, zeta, **kwds):
    """Round difficult input values for Nolan's method in [NO]."""
    x_tol_near_zeta = kwds.get("piecewise_x_tol_near_zeta", 0.005)
    alpha_tol_near_one = kwds.get("piecewise_alpha_tol_near_one", 0.005)

    # following Nolan's STABLE,
    #   "1. When 0 < |alpha-1| < 0.005, the program has numerical problems
    #   evaluating the pdf and cdf.  The current version of the program sets
    #   alpha=1 in these cases. This approximation is not bad in the S0
    #   parameterization."
    if np.abs(alpha - 1) < alpha_tol_near_one:
        alpha = 1.0

    #   "2. When alpha=1 and |beta| < 0.005, the program has numerical
    #   problems.  The current version sets beta=0."
    # We seem to have addressed this through re-expression of g(theta) here

    #   "8. When |x0-beta*tan(pi*alpha/2)| is small, the
    #   computations of the density and cumulative have numerical problems.
    #   The program works around this by setting
    #   z = beta*tan(pi*alpha/2) when
    #   |z-beta*tan(pi*alpha/2)| < tol(5)*alpha**(1/alpha).
    #   (The bound on the right is ad hoc, to get reasonable behavior
    #   when alpha is small)."
    # where tol(5) = 0.5e-2 by default.
    #
    # We seem to have partially addressed this through re-expression of
    # g(theta) here, but it still needs to be used in some extreme cases.
    # Perhaps tol(5) = 0.5e-2 could be reduced for our implementation.
    if np.abs(x0 - zeta) < x_tol_near_zeta * alpha ** (1 / alpha):
        x0 = zeta

    return x0, alpha, beta


def _nolan_g(alpha, beta, x0, xi, zeta):
    """Special function from Nolan's method in [NO]."""
    if alpha != 1:
        # g gets called many times in QUADPACK, so we avoid recomputation here
        # this significantly improves performance of the PDF/CDF integration
        zeta_prefactor = (zeta ** 2 + 1) ** (-1 / (2 * (alpha - 1)))
        alpha_exp = alpha / (alpha - 1)
        alpha_xi = np.arctan(-zeta)
        zeta_offset = x0 - zeta

        def g(theta):
            if theta == -xi:
                return 0 if alpha < 1 else np.inf
            elif theta == np.pi / 2:
                return np.inf if alpha < 1 else 0

            cos_theta = np.cos(theta)
            return (
                zeta_prefactor
                * (cos_theta / np.sin(alpha_xi + alpha * theta) * zeta_offset)
                ** alpha_exp
                * np.cos(alpha_xi + (alpha - 1) * theta)
                / cos_theta
            )

    else:
        # g gets called many times in QUADPACK, so we avoid recomputation here
        # this significantly improves performance of the PDF/CDF integration
        two_beta_div_pi = 2.0 * beta / np.pi
        pi_div_two_beta = 1 / two_beta_div_pi
        x0_div_term = x0 / two_beta_div_pi

        def g(theta):
            if theta == -xi:
                return 0
            elif theta == np.pi / 2:
                return np.inf

            return (
                (1 + theta * two_beta_div_pi)
                * np.exp(
                    (pi_div_two_beta + theta) * np.tan(theta) - x0_div_term
                )
                / np.cos(theta)
            )

    return g


def _nolan_c1(alpha, xi):
    """Special function from Nolan's method in [NO]."""
    if alpha != 1:
        return 0.5 - xi / np.pi if alpha < 1 else 1
    else:
        return 0


def _nolan_c2(alpha, beta, x0, zeta):
    """Special function from Nolan's method in [NO]."""
    if alpha != 1:
        return alpha / np.pi / np.abs(alpha - 1) / (x0 - zeta)
    else:
        return 1.0 / np.abs(beta) / 2.0


def _nolan_c3(alpha):
    """Special function from Nolan's method in [NO]."""
    if alpha != 1:
        return np.sign(1 - alpha) / np.pi
    else:
        return 1 / np.pi


def _pdf_single_value_piecewise(x, alpha, beta, **kwds):
    """Calculate pdf using Nolan's methods as detailed in [NO].
    """
    quad_eps = kwds.get("quad_eps", _QUAD_EPS)

    zeta = -beta * np.tan(np.pi * alpha / 2.0)
    xi = np.arctan(-zeta) / alpha if alpha != 1 else np.pi / 2

    # convert to S_0 parameterization
    x0 = x + zeta if alpha != 1 else x

    x0, alpha, beta = _nolan_round_difficult_input(
        x0, alpha, beta, zeta, **kwds
    )

    # handle Nolan's initial case logic with
    # some other known distribution pdfs / analytical cases
    # TODO: add more where possible with test coverage,
    # eg https://en.wikipedia.org/wiki/Stable_distribution#Other_analytic_cases
    if alpha == 2.0:
        # normal
        return _norm_pdf(x / np.sqrt(2)) / np.sqrt(2)
    elif alpha == 0.5 and beta == 1.0:
        # levy
        # since S(1/2, 1, γ, δ; <x>) == S(1/2, 1, γ, δ+γ; <x0>).
        _x = x0 + 1
        return 1 / np.sqrt(2 * np.pi * _x) / _x * np.exp(-1 / (2 * _x))
    elif alpha == 0.5 and beta == 0.0 and x0 != 0:
        # analytical solution [HO]
        S, C = sc.fresnel([1 / np.sqrt(2 * np.pi * np.abs(x0))])
        arg = 1 / (4 * np.abs(x0))
        return (
            np.sin(arg) * (0.5 - S[0]) + np.cos(arg) * (0.5 - C[0])
        ) / np.sqrt(2 * np.pi * np.abs(x0) ** 3)
    elif alpha == 1.0 and beta == 0.0:
        # cauchy
        return 1 / (1 + x ** 2) / np.pi
    elif x0 == zeta:
        return (
            sc.gamma(1 + 1 / alpha)
            * np.cos(xi)
            / np.pi
            / ((1 + zeta ** 2) ** (1 / alpha / 2))
        )
    elif x0 < zeta:
        return _pdf_single_value_piecewise(-x, alpha, -beta, **kwds)

    # following Nolan, we may now assume
    #   x0 > zeta when alpha != 1
    #   beta != 0 when alpha == 1

    c2 = _nolan_c2(alpha, beta, x0, zeta)
    g = _nolan_g(alpha, beta, x0, xi, zeta)

    # spare calculating integral on null set
    # use isclose as macos has fp differences
    if np.isclose(-xi, np.pi / 2, rtol=1e-014, atol=1e-014):
        return 0.0

    def integrand(theta):
        # limit any numerical issues leading to g_1 < 0 near theta limits
        g_1 = max(g(theta), 0)
        return g_1 * np.exp(-g_1)

    with np.errstate(all="ignore"):
        peak = optimize.bisect(lambda t: g(t) - 1, -xi, np.pi / 2)

        # this integrand can be very peaked, so we need to force
        # QUADPACK to evaluate the function inside its support
        #
        # g(theta) > 300 log(10) or g(theta) < 1e-300 will make
        #   g(theta) * exp(-g(theta)) ~ 1e-300
        # (note doubles 1e-324 and smaller are exactly equal to 0.0)
        exponent_upper_limit = 300 * np.log(10)
        exponent_lower_limit = 1e-300

        # since g is monotonic, we know the direction of increase/decrease
        if g(-xi) > exponent_upper_limit > g(peak):
            left_support = optimize.bisect(
                lambda t: g(t) - exponent_upper_limit, -xi, peak
            )
            right_support = optimize.bisect(
                lambda t: g(t) - exponent_lower_limit, peak, np.pi / 2
            )
        elif g(np.pi / 2) > exponent_upper_limit > g(peak):
            left_support = optimize.bisect(
                lambda t: g(t) - exponent_lower_limit, -xi, peak
            )
            right_support = optimize.bisect(
                lambda t: g(t) - exponent_upper_limit, peak, np.pi / 2
            )
        else:
            # fall back to full integration bounds
            # in case of numerical difficulties
            left_support = -xi
            right_support = np.pi / 2

        # lastly, we add additional samples at
        #   ~exp(-100), ~exp(-10), ~exp(-5), ~exp(-1)
        # to improve QUADPACK's detection of rapidly descending tail behavior
        # (this choice is fairly ad hoc)
        tail_points = [
            optimize.bisect(lambda t: g(t) - exp_height, -xi, np.pi / 2)
            for exp_height in [100, 10, 5]
            # exp_height = 1 is handled by peak
        ]
        intg_points = [left_support, peak, right_support] + tail_points
        intg = integrate.quad(
            integrand,
            left_support,
            right_support,
            points=intg_points,
            limit=100,
            epsrel=quad_eps,
            epsabs=0
        )[0]

    return c2 * intg


def _cdf_single_value_piecewise(x, alpha, beta, **kwds):
    """Calculate cdf using Nolan's methods as detailed in [NO].
    """
    quad_eps = kwds.get("quad_eps", _QUAD_EPS)

    zeta = -beta * np.tan(np.pi * alpha / 2.0)
    if alpha != 1:
        x0 = x + zeta  # convert to S_0 parameterization
        xi = np.arctan(-zeta) / alpha

        if x0 > zeta:
            c_1 = 1 if alpha > 1 else 0.5 - xi / np.pi

            def V(theta):
                return (
                    np.cos(alpha * xi) ** (1 / (alpha - 1))
                    * (np.cos(theta) / np.sin(alpha * (xi + theta)))
                    ** (alpha / (alpha - 1))
                    * (
                        np.cos(alpha * xi + (alpha - 1) * theta)
                        / np.cos(theta)
                    )
                )

            def f(theta):
                z = np.complex128(x0 - zeta)
                return np.exp(-V(theta) * np.real(z ** (alpha / (alpha - 1))))

            with np.errstate(all="ignore"):
                # spare calculating integral on null set
                # use isclose as macos has fp differences
                if np.isclose(-xi, np.pi / 2, rtol=1e-014, atol=1e-014):
                    intg = 0
                else:
                    intg = integrate.quad(f, -xi, np.pi / 2, epsrel=quad_eps,
                                          epsabs=0)[0]
                return c_1 + np.sign(1 - alpha) * intg / np.pi
        elif x0 == zeta:
            return 0.5 - xi / np.pi
        else:
            return 1 - _cdf_single_value_piecewise(-x, alpha, -beta, **kwds)

    else:
        # since location zero, no need to reposition x for S_0
        # parameterization
        xi = np.pi / 2
        if beta > 0:

            def V(theta):
                expr_1 = np.pi / 2 + beta * theta
                return (
                    2.0
                    * expr_1
                    * np.exp(expr_1 * np.tan(theta) / beta)
                    / np.cos(theta)
                    / np.pi
                )

            with np.errstate(all="ignore"):
                expr_1 = np.exp(-np.pi * x / beta / 2.0)
                int_1 = integrate.quad(
                    lambda theta: np.exp(-expr_1 * V(theta)),
                    -np.pi / 2,
                    np.pi / 2,
                    epsrel=quad_eps,
                    epsabs=0
                )[0]
                return int_1 / np.pi
        elif beta == 0:
            return 0.5 + np.arctan(x) / np.pi
        else:
            # NOTE: Nolan's paper has a typo here!
            # He states F(x) = 1 - F(x, alpha, -beta), but this is clearly
            # incorrect since F(-infty) would be 1.0 in this case
            # Indeed, the alpha != 1, x0 < zeta case is correct here.
            return 1 - _cdf_single_value_piecewise(-x, 1, -beta, **kwds)


def _cdf_single_value_piecewise_ragibson(x, alpha, beta, **kwds):
    """Calculate cdf using Nolan's methods as detailed in [NO].
    """
    quad_eps = kwds.get("quad_eps", _QUAD_EPS)

    zeta = -beta * np.tan(np.pi * alpha / 2.0)
    xi = np.arctan(-zeta) / alpha if alpha != 1 else np.pi / 2

    # convert to S_0 parameterization
    x0 = x + zeta if alpha != 1 else x

    x0, alpha, beta = _nolan_round_difficult_input(
        x0, alpha, beta, zeta, **kwds
    )

    # handle Nolan's initial case logic
    if alpha == 1:
        if beta == 0:
            return 0.5 + np.arctan(x) / np.pi
        elif beta < 0:
            # NOTE: Nolan's paper has a typo here!
            # He states F(x) = 1 - F(x, alpha, -beta), but this is clearly
            # incorrect since F(-infty) would be 1.0 in this case
            # Indeed, the alpha != 1, x0 < zeta case is correct here.
            return 1 - _cdf_single_value_piecewise_ragibson(-x, alpha, -beta, **kwds)
    elif x0 == zeta:
        return 0.5 - xi / np.pi
    elif x0 < zeta:
        return 1 - _cdf_single_value_piecewise_ragibson(-x, alpha, -beta, **kwds)

    # following Nolan, we may now assume
    #   x0 > zeta when alpha != 1
    #   beta > 0 when alpha == 1

    c1 = _nolan_c1(alpha, xi)
    c3 = _nolan_c3(alpha)
    g = _nolan_g(alpha, beta, x0, xi, zeta)

    # spare calculating integral on null set
    # use isclose as macos has fp differences
    if np.isclose(-xi, np.pi / 2, rtol=1e-014, atol=1e-014):
        return c1

    def integrand(theta):
        # limit any numerical issues leading to g_1 < 0 near theta limits
        g_1 = max(g(theta), 0)
        return np.exp(-g_1)

    with np.errstate(all="ignore"):
        left_support = -xi
        right_support = np.pi / 2

        # this integrand can drop very quickly, so we need to force
        # QUADPACK to evaluate the function inside its support
        #
        # g(theta) > 300 log(10) will make
        #   exp(-g(theta)) ~ 1e-300
        # (note doubles 1e-324 and smaller are exactly equal to 0.0)
        exponent_upper_limit = 300 * np.log(10)

        # since g is monotonic, we know the direction of increase/decrease
        if g(-xi) > g(np.pi / 2):
            left_support = optimize.bisect(
                lambda t: g(t) - exponent_upper_limit, -xi, np.pi / 2
            )
        elif g(np.pi / 2) > g(-xi):
            right_support = optimize.bisect(
                lambda t: g(t) - exponent_upper_limit, -xi, np.pi / 2
            )

        intg = integrate.quad(
            integrand,
            left_support,
            right_support,
            points=[left_support, right_support],
            limit=100,
            epsrel=quad_eps,
            epsabs=0
        )[0]

    return c1 + c3 * intg


class levy_stable_gen(rv_continuous):
    r"""A Levy-stable continuous random variable.

    %(before_notes)s

    See Also
    --------
    levy, levy_l

    Notes
    -----
    The distribution for `levy_stable` has characteristic function:

    .. math::

        \varphi(t, \alpha, \beta, c, \mu) =
        e^{it\mu -|ct|^{\alpha}(1-i\beta\operatorname{sign}(t)\Phi(\alpha, t))}

    where:

    .. math::

        \Phi = \begin{cases}
                \tan \left({\frac {\pi \alpha }{2}}\right)&\alpha \neq 1\\
                -{\frac {2}{\pi }}\log |t|&\alpha =1
                \end{cases}

    The probability density function for `levy_stable` is:

    .. math::

        f(x) = \frac{1}{2\pi}\int_{-\infty}^\infty \varphi(t)e^{-ixt}\,dt

    where :math:`-\infty < t < \infty`. This integral does not have a known
    closed form.

    For evaluation of pdf we use either Nolan's piecewise approach using
    Zolotarev :math:`S_0` parameterization with integration, direct numerical
    integration of standard parameterization of characteristic function or FFT
    of characteristic function.

    The default method is 'piecewise' which uses Nolan's piecewise method. The
    default method can be changed by setting ``levy_stable.pdf_default_method``
    to either 'piecewise', 'dni' or 'fft-simpson'.

    To improve performance of piecewise and direct numerical integration one
    can specify ``levy_stable.quad_eps`` (defaults to 1.2e-14). This is used
    as the absolute and relative quadrature tolerances for direct numerical
    integration and the relative quadrature tolerance for the piecewise method.
    One can also specify ``levy_stable.piecewise_x_tol_near_zeta`` (defaults to
    0.005) for how close x is to zeta before it is considered the same as x
    [NO]. The exact check is
    ``abs(x0 - zeta) < piecewise_x_tol_near_zeta*alpha**(1/alpha)``. One can
    also specify ``levy_stable.piecewise_alpha_tol_near_one`` (defaults to
    0.005) for how close alpha is to 1 before being considered equal to 1.

    To increase accuracy of FFT calculation one can specify
    ``levy_stable.pdf_fft_grid_spacing`` (defaults to 0.001) and
    ``pdf_fft_n_points_two_power`` (defaults to None which means a value is
    calculated that sufficiently covers the input range).

    Further control over FFT calculation is available by setting
    ``pdf_fft_interpolation_kind`` (defaults to 3) for spline order and
    ``pdf_fft_interpolation_level`` for determine number of points for
    Newton-Cote formula when approximating the characteristic function
    (considered experimental).

    For evaluation of cdf we use Nolan's piecewise approach using Zolatarev
    :math:`S_0` parameterization with integration or integral of the pdf FFT
    interpolated spline. The settings affecting FFT calculation are the same as
    for pdf calculation. The default cdf method can be changed by setting
    ``levy_stable.cdf_default_method`` to either 'piecewise' or 'fft-simpson'.
    For cdf calculations the Zolatarev method is superior in accuracy, so FFT
    is disabled by default.

    Fitting estimate uses quantile estimation method in [MC]. MLE estimation of
    parameters in fit method uses this quantile estimate initially. Note that
    MLE doesn't always converge if using FFT for pdf calculations; this will be
    the case if alpha <= 1 where the FFT approach doesn't give good
    approximations.

    Any non-missing value for the attribute
    ``levy_stable.pdf_fft_min_points_threshold`` will set
    ``levy_stable.pdf_default_method`` to 'fft-simpson'.

    The pdf methods 'best' and 'zolotarev' are equivalent to 'piecewise'. The
    pdf method 'quadrature' is equivalent to 'dni'.

    .. warning::

        For pdf calculations FFT calculation is considered experimental.

        For cdf calculations FFT calculation is considered experimental. Use
        Zolatarev's method instead (default).

    %(after_notes)s

    References
    ----------
    .. [MC] McCulloch, J., 1986. Simple consistent estimators of stable
        distribution parameters. Communications in Statistics - Simulation and
        Computation 15, 11091136.
    .. [WZ] Wang, Li and Zhang, Ji-Hong, 2008. Simpson’s rule based FFT method
        to compute densities of stable distribution.
    .. [NO] Nolan, J., 1997. Numerical Calculation of Stable Densities and
        distributions Functions.
    .. [HO] Hopcraft, K. I., Jakeman, E., Tanner, R. M. J., 1999. Lévy random
        walks with fluctuating step number and multiscale behavior.

    %(example)s

    """

    def _rvs(self, alpha, beta, size=None, random_state=None):
        def alpha1func(alpha, beta, TH, aTH, bTH, cosTH, tanTH, W):
            return 2 / np.pi * (np.pi / 2 + bTH) * tanTH - beta * np.log(
                (np.pi / 2 * W * cosTH) / (np.pi / 2 + bTH)
            )

        def beta0func(alpha, beta, TH, aTH, bTH, cosTH, tanTH, W):
            return (
                W
                / (cosTH / np.tan(aTH) + np.sin(TH))
                * ((np.cos(aTH) + np.sin(aTH) * tanTH) / W) ** (1.0 / alpha)
            )

        def otherwise(alpha, beta, TH, aTH, bTH, cosTH, tanTH, W):
            # alpha is not 1 and beta is not 0
            val0 = beta * np.tan(np.pi * alpha / 2)
            th0 = np.arctan(val0) / alpha
            val3 = W / (cosTH / np.tan(alpha * (th0 + TH)) + np.sin(TH))
            res3 = val3 * (
                (
                    np.cos(aTH)
                    + np.sin(aTH) * tanTH
                    - val0 * (np.sin(aTH) - np.cos(aTH) * tanTH)
                )
                / W
            ) ** (1.0 / alpha)
            return res3

        def alphanot1func(alpha, beta, TH, aTH, bTH, cosTH, tanTH, W):
            res = _lazywhere(
                beta == 0,
                (alpha, beta, TH, aTH, bTH, cosTH, tanTH, W),
                beta0func,
                f2=otherwise,
            )
            return res

        alpha = np.broadcast_to(alpha, size)
        beta = np.broadcast_to(beta, size)
        TH = uniform.rvs(
            loc=-np.pi / 2.0, scale=np.pi, size=size, random_state=random_state
        )
        W = expon.rvs(size=size, random_state=random_state)
        aTH = alpha * TH
        bTH = beta * TH
        cosTH = np.cos(TH)
        tanTH = np.tan(TH)
        res = _lazywhere(
            alpha == 1,
            (alpha, beta, TH, aTH, bTH, cosTH, tanTH, W),
            alpha1func,
            f2=alphanot1func,
        )
        return res

    def _argcheck(self, alpha, beta):
        return (alpha > 0) & (alpha <= 2) & (beta <= 1) & (beta >= -1)

    def _pdf(self, x, alpha, beta):

        x = np.asarray(x).reshape(1, -1)[0, :]

        x, alpha, beta = np.broadcast_arrays(x, alpha, beta)

        data_in = np.dstack((x, alpha, beta))[0]
        data_out = np.empty(shape=(len(data_in), 1))

        pdf_default_method_name = getattr(
            self, "pdf_default_method", "piecewise"
        )
        if pdf_default_method_name in ("piecewise", "best", "zolotarev"):
            pdf_single_value_method = _pdf_single_value_piecewise
        elif pdf_default_method_name in ("dni", "quadrature"):
            pdf_single_value_method = _pdf_single_value_cf_integrate
        elif (
            pdf_default_method_name == "fft-simpson"
            or getattr(self, "pdf_fft_min_points_threshold", None) is not None
        ):
            pdf_single_value_method = None

        pdf_single_value_kwds = {
            "quad_eps": getattr(self, "quad_eps", _QUAD_EPS),
            "piecewise_x_tol_near_zeta": getattr(
                self, "piecewise_x_tol_near_zeta", 0.005
            ),
            "piecewise_alpha_tol_near_one": getattr(
                self, "piecewise_alpha_tol_near_one", 0.005
            )
        }

        fft_grid_spacing = getattr(self, "pdf_fft_grid_spacing", 0.001)
        fft_n_points_two_power = getattr(
            self, "pdf_fft_n_points_two_power", None
        )
        fft_interpolation_level = getattr(
            self, "pdf_fft_interpolation_level", 3
        )
        fft_interpolation_kind = getattr(self, "pdf_fft_interpolation_kind", 3)

        # group data in unique arrays of alpha, beta pairs
        uniq_param_pairs = np.unique(data_in[:, 1:], axis=0)
        for pair in uniq_param_pairs:
            data_mask = np.all(data_in[:, 1:] == pair, axis=-1)
            data_subset = data_in[data_mask]
            if pdf_single_value_method is not None:
                data_out[data_mask] = np.array(
                    [
                        pdf_single_value_method(
                            _x, _alpha, _beta, **pdf_single_value_kwds
                        )
                        for _x, _alpha, _beta in data_subset
                    ]
                ).reshape(len(data_subset), 1)
            else:
                warnings.warn(
                    "Density calculations experimental for FFT method."
                    + " Use combination of piecewise and dni methods instead.",
                    RuntimeWarning,
                )
                _alpha, _beta = pair
                _x = data_subset[:, (0,)]

                if _alpha < 1.0:
                    raise RuntimeError(
                        "FFT method does not work well for alpha less than 1."
                    )

                # need enough points to "cover" _x for interpolation
                if fft_grid_spacing is None and fft_n_points_two_power is None:
                    raise ValueError(
                        "One of fft_grid_spacing or fft_n_points_two_power "
                        + "needs to be set."
                    )
                max_abs_x = np.max(np.abs(_x))
                h = (
                    2 ** (3 - fft_n_points_two_power) * max_abs_x
                    if fft_grid_spacing is None
                    else fft_grid_spacing
                )
                q = (
                    np.ceil(np.log(2 * max_abs_x / h) / np.log(2)) + 2
                    if fft_n_points_two_power is None
                    else int(fft_n_points_two_power)
                )

                # for some parameters, the range of x can be quite
                # large, let's choose an arbitrary cut off (8GB) to save on
                # computer memory.
                MAX_Q = 30
                if q > MAX_Q:
                    raise RuntimeError(
                        "fft_n_points_two_power has a maximum "
                        + f"value of {MAX_Q}"
                    )

                density_x, density = pdf_from_cf_with_fft(
                    lambda t: _cf(t, _alpha, _beta),
                    h=h,
                    q=q,
                    level=fft_interpolation_level,
                )
                f = interpolate.interp1d(
                    density_x, np.real(density), kind=fft_interpolation_kind
                )  # patch FFT to use cubic
                data_out[data_mask] = f(_x)

        return data_out.T[0]

    def _cdf(self, x, alpha, beta):

        x = np.asarray(x).reshape(1, -1)[0, :]

        x, alpha, beta = np.broadcast_arrays(x, alpha, beta)

        data_in = np.dstack((x, alpha, beta))[0]
        data_out = np.empty(shape=(len(data_in), 1))

        cdf_default_method_name = getattr(
            self, "cdf_default_method", "piecewise"
        )
        if cdf_default_method_name == "piecewise":
            cdf_single_value_method = _cdf_single_value_piecewise_ragibson
        elif cdf_default_method_name == "fft-simpson":
            cdf_single_value_method = None

        cdf_single_value_kwds = {
            "quad_eps": getattr(self, "quad_eps", _QUAD_EPS),
            "piecewise_x_tol_near_zeta": getattr(
                self, "piecewise_x_tol_near_zeta", 0.005
            ),
            "piecewise_alpha_tol_near_one": getattr(
                self, "piecewise_alpha_tol_near_one", 0.005
            )
        }

        fft_grid_spacing = getattr(self, "pdf_fft_grid_spacing", 0.001)
        fft_n_points_two_power = getattr(
            self, "pdf_fft_n_points_two_power", None
        )
        fft_interpolation_level = getattr(
            self, "pdf_fft_interpolation_level", 3
        )
        fft_interpolation_kind = getattr(self, "pdf_fft_interpolation_kind", 3)

        # group data in unique arrays of alpha, beta pairs
        uniq_param_pairs = np.unique(data_in[:, 1:], axis=0)
        for pair in uniq_param_pairs:
            data_mask = np.all(data_in[:, 1:] == pair, axis=-1)
            data_subset = data_in[data_mask]
            if cdf_single_value_method is not None:
                data_out[data_mask] = np.array(
                    [
                        cdf_single_value_method(
                            _x, _alpha, _beta, **cdf_single_value_kwds
                        )
                        for _x, _alpha, _beta in data_subset
                    ]
                ).reshape(len(data_subset), 1)
            else:
                warnings.warn(
                    "Cumulative density calculations experimental for FFT method."
                    + " Use piecewise method instead.",
                    RuntimeWarning,
                )
                _alpha, _beta = pair
                _x = data_subset[:, (0,)]

                # need enough points to "cover" _x for interpolation
                if fft_grid_spacing is None and fft_n_points_two_power is None:
                    raise ValueError(
                        "One of fft_grid_spacing or fft_n_points_two_power "
                        + "needs to be set."
                    )
                max_abs_x = np.max(np.abs(_x))
                h = (
                    2 ** (3 - fft_n_points_two_power) * max_abs_x
                    if fft_grid_spacing is None
                    else fft_grid_spacing
                )
                q = (
                    np.ceil(np.log(2 * max_abs_x / h) / np.log(2)) + 2
                    if fft_n_points_two_power is None
                    else int(fft_n_points_two_power)
                )

                density_x, density = pdf_from_cf_with_fft(
                    lambda t: _cf(t, _alpha, _beta),
                    h=h,
                    q=q,
                    level=fft_interpolation_level,
                )
                f = interpolate.InterpolatedUnivariateSpline(
                    density_x, np.real(density)
                )
                data_out[data_mask] = np.array(
                    [f.integral(self.a, x_1) for x_1 in _x]
                ).reshape(data_out[data_mask].shape)

        return data_out.T[0]

    def _fitstart(self, data):
        # We follow McCullock 1986 method - Simple Consistent Estimators
        # of Stable Distribution Parameters

        # fmt: off
        # Table III and IV
        nu_alpha_range = [2.439, 2.5, 2.6, 2.7, 2.8, 3, 3.2, 3.5, 4,
                          5, 6, 8, 10, 15, 25]
        nu_beta_range = [0, 0.1, 0.2, 0.3, 0.5, 0.7, 1]

        # table III - alpha = psi_1(nu_alpha, nu_beta)
        alpha_table = [
            [2.000, 2.000, 2.000, 2.000, 2.000, 2.000, 2.000],
            [1.916, 1.924, 1.924, 1.924, 1.924, 1.924, 1.924],
            [1.808, 1.813, 1.829, 1.829, 1.829, 1.829, 1.829],
            [1.729, 1.730, 1.737, 1.745, 1.745, 1.745, 1.745],
            [1.664, 1.663, 1.663, 1.668, 1.676, 1.676, 1.676],
            [1.563, 1.560, 1.553, 1.548, 1.547, 1.547, 1.547],
            [1.484, 1.480, 1.471, 1.460, 1.448, 1.438, 1.438],
            [1.391, 1.386, 1.378, 1.364, 1.337, 1.318, 1.318],
            [1.279, 1.273, 1.266, 1.250, 1.210, 1.184, 1.150],
            [1.128, 1.121, 1.114, 1.101, 1.067, 1.027, 0.973],
            [1.029, 1.021, 1.014, 1.004, 0.974, 0.935, 0.874],
            [0.896, 0.892, 0.884, 0.883, 0.855, 0.823, 0.769],
            [0.818, 0.812, 0.806, 0.801, 0.780, 0.756, 0.691],
            [0.698, 0.695, 0.692, 0.689, 0.676, 0.656, 0.597],
            [0.593, 0.590, 0.588, 0.586, 0.579, 0.563, 0.513]]

        # table IV - beta = psi_2(nu_alpha, nu_beta)
        beta_table = [
            [0, 2.160, 1.000, 1.000, 1.000, 1.000, 1.000],
            [0, 1.592, 3.390, 1.000, 1.000, 1.000, 1.000],
            [0, 0.759, 1.800, 1.000, 1.000, 1.000, 1.000],
            [0, 0.482, 1.048, 1.694, 1.000, 1.000, 1.000],
            [0, 0.360, 0.760, 1.232, 2.229, 1.000, 1.000],
            [0, 0.253, 0.518, 0.823, 1.575, 1.000, 1.000],
            [0, 0.203, 0.410, 0.632, 1.244, 1.906, 1.000],
            [0, 0.165, 0.332, 0.499, 0.943, 1.560, 1.000],
            [0, 0.136, 0.271, 0.404, 0.689, 1.230, 2.195],
            [0, 0.109, 0.216, 0.323, 0.539, 0.827, 1.917],
            [0, 0.096, 0.190, 0.284, 0.472, 0.693, 1.759],
            [0, 0.082, 0.163, 0.243, 0.412, 0.601, 1.596],
            [0, 0.074, 0.147, 0.220, 0.377, 0.546, 1.482],
            [0, 0.064, 0.128, 0.191, 0.330, 0.478, 1.362],
            [0, 0.056, 0.112, 0.167, 0.285, 0.428, 1.274]]

        # Table V and VII
        alpha_range = [2, 1.9, 1.8, 1.7, 1.6, 1.5, 1.4, 1.3, 1.2, 1.1,
                       1, 0.9, 0.8, 0.7, 0.6, 0.5]
        beta_range = [0, 0.25, 0.5, 0.75, 1]

        # Table V - nu_c = psi_3(alpha, beta)
        nu_c_table = [
            [1.908, 1.908, 1.908, 1.908, 1.908],
            [1.914, 1.915, 1.916, 1.918, 1.921],
            [1.921, 1.922, 1.927, 1.936, 1.947],
            [1.927, 1.930, 1.943, 1.961, 1.987],
            [1.933, 1.940, 1.962, 1.997, 2.043],
            [1.939, 1.952, 1.988, 2.045, 2.116],
            [1.946, 1.967, 2.022, 2.106, 2.211],
            [1.955, 1.984, 2.067, 2.188, 2.333],
            [1.965, 2.007, 2.125, 2.294, 2.491],
            [1.980, 2.040, 2.205, 2.435, 2.696],
            [2.000, 2.085, 2.311, 2.624, 2.973],
            [2.040, 2.149, 2.461, 2.886, 3.356],
            [2.098, 2.244, 2.676, 3.265, 3.912],
            [2.189, 2.392, 3.004, 3.844, 4.775],
            [2.337, 2.634, 3.542, 4.808, 6.247],
            [2.588, 3.073, 4.534, 6.636, 9.144]]

        # Table VII - nu_zeta = psi_5(alpha, beta)
        nu_zeta_table = [
            [0, 0.000, 0.000, 0.000, 0.000],
            [0, -0.017, -0.032, -0.049, -0.064],
            [0, -0.030, -0.061, -0.092, -0.123],
            [0, -0.043, -0.088, -0.132, -0.179],
            [0, -0.056, -0.111, -0.170, -0.232],
            [0, -0.066, -0.134, -0.206, -0.283],
            [0, -0.075, -0.154, -0.241, -0.335],
            [0, -0.084, -0.173, -0.276, -0.390],
            [0, -0.090, -0.192, -0.310, -0.447],
            [0, -0.095, -0.208, -0.346, -0.508],
            [0, -0.098, -0.223, -0.380, -0.576],
            [0, -0.099, -0.237, -0.424, -0.652],
            [0, -0.096, -0.250, -0.469, -0.742],
            [0, -0.089, -0.262, -0.520, -0.853],
            [0, -0.078, -0.272, -0.581, -0.997],
            [0, -0.061, -0.279, -0.659, -1.198]]
        # fmt: on

        psi_1 = interpolate.interp2d(
            nu_beta_range, nu_alpha_range, alpha_table, kind="linear"
        )
        psi_2 = interpolate.interp2d(
            nu_beta_range, nu_alpha_range, beta_table, kind="linear"
        )
        psi_2_1 = lambda nu_beta, nu_alpha: (
            psi_2(nu_beta, nu_alpha)
            if nu_beta > 0
            else -psi_2(-nu_beta, nu_alpha)
        )

        phi_3 = interpolate.interp2d(
            beta_range, alpha_range, nu_c_table, kind="linear"
        )
        phi_3_1 = lambda beta, alpha: (
            phi_3(beta, alpha) if beta > 0 else phi_3(-beta, alpha)
        )
        phi_5 = interpolate.interp2d(
            beta_range, alpha_range, nu_zeta_table, kind="linear"
        )
        phi_5_1 = lambda beta, alpha: (
            phi_5(beta, alpha) if beta > 0 else -phi_5(-beta, alpha)
        )

        # quantiles
        p05 = np.percentile(data, 5)
        p50 = np.percentile(data, 50)
        p95 = np.percentile(data, 95)
        p25 = np.percentile(data, 25)
        p75 = np.percentile(data, 75)

        nu_alpha = (p95 - p05) / (p75 - p25)
        nu_beta = (p95 + p05 - 2 * p50) / (p95 - p05)

        if nu_alpha >= 2.439:
            alpha = np.clip(
                psi_1(nu_beta, nu_alpha)[0], np.finfo(float).eps, 2.0
            )
            beta = np.clip(psi_2_1(nu_beta, nu_alpha)[0], -1.0, 1.0)
        else:
            alpha = 2.0
            beta = np.sign(nu_beta)
        c = (p75 - p25) / phi_3_1(beta, alpha)[0]
        zeta = p50 + c * phi_5_1(beta, alpha)[0]
        delta = np.clip(
            zeta - beta * c * np.tan(np.pi * alpha / 2.0)
            if alpha == 1.0
            else zeta,
            np.finfo(float).eps,
            np.inf,
        )

        return (alpha, beta, delta, c)

    def _stats(self, alpha, beta):
        mu = 0 if alpha > 1 else np.nan
        mu2 = 2 if alpha == 2 else np.inf
        g1 = 0.0 if alpha == 2.0 else np.NaN
        g2 = 0.0 if alpha == 2.0 else np.NaN
        return mu, mu2, g1, g2


levy_stable = levy_stable_gen(name="levy_stable")
