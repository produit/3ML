__author__='grburgess'

import collections
import copy
import os

import numpy as np
import pandas as pd
from pandas import HDFStore

from threeML.config.config import threeML_config
from threeML.exceptions.custom_exceptions import custom_warnings
from threeML.io.file_utils import sanitize_filename
from threeML.io.progress_bar import progress_bar
from threeML.io.rich_display import display
from threeML.utils.binner import TemporalBinner
from threeML.utils.time_interval import TimeIntervalSet
from threeML.utils.time_series.polynomial import polyfit, unbinned_polyfit, Polynomial
from threeML.plugins.OGIP.response import InstrumentResponse
from threeML.plugins.spectrum.binned_spectrum import Quality

class ReducingNumberOfThreads(Warning):
    pass


class ReducingNumberOfSteps(Warning):
    pass


class OverLappingIntervals(RuntimeError):
    pass


# find out how many splits we need to make
def ceildiv(a, b):
    return -(-a // b)


class TimeSeries(object):
    def __init__(self, start_time,stop_time, n_channels ,native_quality=None,
                 first_channel=1, ra=None, dec=None, mission=None, instrument=None, verbose=True):
        """
        The EventList is a container for event data that is tagged in time and in PHA/energy. It handles event selection,
        temporal polynomial fitting, temporal binning, and exposure calculations (in subclasses). Once events are selected
        and/or polynomials are fit, the selections can be extracted via a PHAContainer which is can be read by an OGIPLike
        instance and translated into a PHA instance.


        :param  n_channels: Number of detector channels
        :param  start_time: start time of the event list
        :param  stop_time: stop time of the event list
        :param  first_channel: where detchans begin indexing
        :param  rsp_file: the response file corresponding to these events
        :param  arrival_times: list of event arrival times
        :param  energies: list of event energies or pha channels
        :param native_quality: native pha quality flags
        :param mission:
        :param instrument:
        :param verbose:
        :param  ra:
        :param  dec:
        """

        self._verbose = verbose
        self._n_channels = n_channels
        self._first_channel = first_channel
        self._native_quality = native_quality


        # we haven't made selections yet

        self._time_intervals = None
        self._poly_intervals = None
        self._counts = None
        self._poly_counts = None
        self._poly_count_err= None


        if native_quality is not None:

            assert len(native_quality) == n_channels, "the native quality has length %d but you specified there were %d channels"%(len(native_quality), n_channels)


        self._start_time = start_time

        self._stop_time = stop_time

        # name the instrument if there is not one

        if instrument is None:

            custom_warnings.warn('No instrument name is given. Setting to UNKNOWN')

            self._instrument = "UNKNOWN"

        else:

            self._instrument = instrument

        if mission is None:

            custom_warnings.warn('No mission name is given. Setting to UNKNOWN')

            self._mission = "UNKNOWN"

        else:

            self._mission = mission



        self._user_poly_order = -1
        self._time_selection_exists = False
        self._poly_fit_exists = False

        self._fit_method_info = {"bin type": None, 'fit method': None}

    def set_active_time_intervals(self, *args):

        raise RuntimeError("Must be implemented in subclass")

    @property
    def poly_fit_exists(self):

        return self._poly_fit_exists

    @property
    def n_channels(self):

        return self._n_channels

    @property
    def poly_intervals(self):
        return self._poly_intervals

    @property
    def polynomials(self):
        """ Returns polynomial is they exist"""
        if self._poly_fit_exists:
            return self._polynomials
        else:
            RuntimeError('A polynomial fit has not been made.')

    def get_poly_info(self):
        """
        Return a pandas panel frame with the polynomial coeffcients
        and errors
        Returns:
            a DataFrame

        """

        if self._poly_fit_exists:

            coeff = []
            err = []

            for poly in self._polynomials:
                coeff.append(poly.coefficients)
                err.append(poly.error)
            df_coeff = pd.DataFrame(coeff)
            df_err = pd.DataFrame(err)

            # print('Coefficients')
            #
            # display(df_coeff)
            #
            # print('Coefficient Error')
            #
            # display(df_err)

            pan = pd.Panel({'coefficients': df_coeff, 'error': df_err})

            return pan


        else:
            RuntimeError('A polynomial fit has not been made.')

    def get_total_poly_count(self, start, stop, mask=None):
        """

        Get the total poly counts

        :param start:
        :param stop:
        :return:
        """
        if mask is None:
            mask = np.ones_like(self._polynomials, dtype=np.bool)

        total_counts = 0

        for p in np.asarray(self._polynomials)[mask]:
            total_counts += p.integral(start, stop)

        return total_counts

    def get_total_poly_error(self, start, stop, mask=None):
        """

        Get the total poly error

        :param start:
        :param stop:
        :return:
        """
        if mask is None:
            mask = np.ones_like(self._polynomials, dtype=np.bool)

        total_counts = 0

        for p in np.asarray(self._polynomials)[mask]:
            total_counts += p.integral_error(start, stop) ** 2

        return np.sqrt(total_counts)

    @property
    def bins(self):

        if self._temporal_binner is not None:

            return self._temporal_binner
        else:

            raise RuntimeError('This EventList has no binning specified')

    def bin_by_significance(self, start, stop, sigma, mask=None, min_counts=1):
        """

       Interface to the temporal binner's significance binning model

        :param start: start of the interval to bin on
        :param stop:  stop of the interval ot bin on
        :param sigma: sigma-level of the bins
        :param mask: (bool) use the energy mask to decide on significance
        :param min_counts:  minimum number of counts per bin
        :return:
        """

        if mask is not None:

            # create phas to check
            phas = np.arange(self._first_channel, self._n_channels)[mask]

            this_mask = np.zeros_like(self._arrival_times, dtype=np.bool)

            for channel in phas:
                this_mask = np.logical_or(this_mask, self._energies == channel)

            events = self._arrival_times[this_mask]

        else:

            events = copy.copy(self._arrival_times)

        events = events[np.logical_and(events <= stop, events >= start)]



        tmp_bkg_getter = lambda a, b: self.get_total_poly_count(a, b, mask)
        tmp_err_getter = lambda a, b: self.get_total_poly_error(a, b, mask)

        # self._temporal_binner.bin_by_significance(tmp_bkg_getter,
        #                                           background_error_getter=tmp_err_getter,
        #                                           sigma_level=sigma,
        #                                           min_counts=min_counts)

        self._temporal_binner = TemporalBinner.bin_by_significance(events,
                                                                   tmp_bkg_getter,
                                                                   background_error_getter=tmp_err_getter,
                                                                   sigma_level=sigma,
                                                                   min_counts=min_counts)

    def bin_by_constant(self, start, stop, dt=1):
        """
        Interface to the temporal binner's constant binning mode

        :param start: start time of the bins
        :param stop: stop time of the bins
        :param dt: temporal spacing of the bins
        :return:
        """

        events = self._arrival_times[np.logical_and(self._arrival_times >= start, self._arrival_times <= stop)]

        self._temporal_binner = TemporalBinner.bin_by_constant(events, dt)

    def bin_by_custom(self, start, stop):
        """
        Interface to temporal binner's custom bin mode


        :param start: start times of the bins
        :param stop:  stop times of the bins
        :return:
        """

        self._temporal_binner = TemporalBinner.bin_by_custom(start, stop)
        #self._temporal_binner.bin_by_custom(start, stop)

    def bin_by_bayesian_blocks(self, start, stop, p0, use_background=False):

        events = self._arrival_times[np.logical_and(self._arrival_times >= start, self._arrival_times <= stop)]

        #self._temporal_binner = TemporalBinner(events)

        if use_background:

            integral_background = lambda t: self.get_total_poly_count(start, t)

            self._temporal_binner = TemporalBinner.bin_by_bayesian_blocks(events,
                                                                          p0,
                                                                          bkg_integral_distribution=integral_background)

        else:

            self._temporal_binner = TemporalBinner.bin_by_bayesian_blocks(events,
                                                                          p0)

    def __set_poly_order(self, value):
        """ Set poly order only in allowed range and redo fit """

        assert type(value) is int, "Polynomial order must be integer"

        assert -1 <= value <= 4, "Polynomial order must be 0-4 or -1 to have it determined"

        self._user_poly_order = value

        if self._poly_fit_exists:

            print('Refitting background with new polynomial order (%d) and existing selections' % value)

            if self._time_selection_exists:

                self.set_polynomial_fit_interval(*self._poly_intervals.to_string().split(','), unbinned=self._unbinned)

            else:

                RuntimeError("This is a bug. Should never get here")

    def ___set_poly_order(self, value):
        """ Indirect poly order setter """

        self.__set_poly_order(value)

    def __get_poly_order(self):
        """ get the poly order """

        return self._optimal_polynomial_grade

    def ___get_poly_order(self):
        """ Indirect poly order getter """

        return self.__get_poly_order()

    poly_order = property(___get_poly_order, ___set_poly_order,
                          doc="Get or set the polynomial order")

    @property
    def time_intervals(self):
        """
        the time intervals of the events

        :return:
        """
        return self._time_intervals

    def exposure_over_interval(self, tmin, tmax):
        """ calculate the exposure over a given interval  """

        raise RuntimeError("Must be implemented in sub class")

    def counts_over_interval(self, start, stop):
        """
        return the number of counts in the selected interval
        :param start: start of interval
        :param stop:  stop of interval
        :return:
        """

        # this will be a boolean list and the sum will be the
        # number of events

        raise RuntimeError("Must be implemented in sub class")

    def set_polynomial_fit_interval(self, *time_intervals, **options):
        """Set the time interval to fit the background.
        Multiple intervals can be input as separate arguments
        Specified as 'tmin-tmax'. Intervals are in seconds. Example:

        set_polynomial_fit_interval("-10.0-0.0","10.-15.")

        :param time_intervals: intervals to fit on
        :param options:

        """

        # Find out if we want to binned or unbinned.
        # TODO: add the option to config file
        if 'unbinned' in options:
            unbinned = options.pop('unbinned')
            assert type(unbinned) == bool, 'unbinned option must be True or False'

        else:

            # assuming unbinned
            # could use config file here
            # unbinned = threeML_config['ogip']['use-unbinned-poly-fitting']

            unbinned = True

        # we create some time intervals

        poly_intervals = TimeIntervalSet.from_strings(*time_intervals)

        # adjust the selections to the data
        for time_interval in poly_intervals:
            t1 = time_interval.start_time
            t2 = time_interval.stop_time

            if t1 < self._start_time:

                custom_warnings.warn(
                    "The time interval %f-%f started before the first arrival time (%f), so we are changing the intervals to %f-%f" % (
                    t1, t2, self._start_time, self._start_time, t2))

                t1 = self._start_time

            if t2 > self._stop_time:

                custom_warnings.warn(
                    "The time interval %f-%f ended after the last arrival time (%f), so we are changing the intervals to %f-%f" % (
                        t1, t2, self._stop_time, t1, self._stop_time))

                t2 = self._stop_time

            if  (self._stop_time <= t1) or (t2 <= self._start_time):
                custom_warnings.warn(
                    "The time interval %f-%f is out side of the arrival times and will be dropped" % (
                        t1, t2))
                continue

        # set the poly intervals as an attribute

        self._poly_intervals = poly_intervals

        # Fit the events with the given intervals
        if unbinned:

            self._unbinned = True  # keep track!

            self._unbinned_fit_polynomials()

        else:

            self._unbinned = False

            self._fit_polynomials()

        # we have a fit now

        self._poly_fit_exists = True

        if self._verbose:
            print("%s %d-order polynomial fit with the %s method" % (
                self._fit_method_info['bin type'], self._optimal_polynomial_grade, self._fit_method_info['fit method']))
            print('\n')

        # recalculate the selected counts

        if self._time_selection_exists:

            self.set_active_time_intervals(*self._time_intervals.to_string().split(','))

    def get_information_dict(self, use_poly=False):
        """
        Return a PHAContainer that can be read by different builders

        :param use_poly: (bool) choose to build from the polynomial fits
        """
        if not self._time_selection_exists:
            raise RuntimeError('No time selection exists! Cannot calculate rates')

        if use_poly:

            is_poisson = False

            counts_err = self._poly_count_err
            counts = self._poly_counts
            rate_err = self._poly_count_err / self._exposure
            rates = self._poly_counts / self._exposure

            # removing negative counts

            idx = counts < 0.

            counts[idx] = 0.
            counts_err[idx] = 0.

            rates[idx] = 0.
            rate_err[idx] = 0.

        else:

            is_poisson = True

            counts_err = None
            counts = self._counts
            rates = self._counts / self._exposure
            rate_err = None


        if self._native_quality is None:

            quality = np.zeros_like(counts, dtype=int)

        else:

            quality = self._native_quality

        container_dict = {}

        container_dict['instrument'] = self._instrument
        container_dict['telescope'] = self._mission
        container_dict['tstart'] = self._time_intervals.absolute_start_time
        container_dict['telapse'] = self._time_intervals.absolute_stop_time - self._time_intervals.absolute_start_time
        container_dict['channel'] = np.arange(self._n_channels) + self._first_channel
        container_dict['counts'] = counts
        container_dict['counts error'] = counts_err
        container_dict['rates'] = rates
        container_dict['rate error'] = rate_err

        # check to see if we already have a quality object

        if isinstance(quality, Quality):

            container_dict['quality'] = quality

        else:

            container_dict['quality'] = Quality.from_ogip(quality)

        # TODO: make sure the grouping makes sense
        container_dict['backfile']='NONE'
        container_dict['grouping'] = np.ones(self._n_channels)
        container_dict['exposure'] = self._exposure
        #container_dict['response'] = self._response

        return container_dict

    def __repr__(self):
        """
        Examine the currently selected info as well other things.

        """


        return self._output().to_string()

    def _output(self):

        info_dict = collections.OrderedDict()
        for i, interval in enumerate(self.time_intervals):
            info_dict['active selection (%d)' % (i + 1)] = interval.__repr__()

        info_dict['active deadtime'] = self._active_dead_time

        if self._poly_fit_exists:

            for i, interval in enumerate(self.poly_intervals):
                info_dict['polynomial selection (%d)' % (i + 1)] = interval.__repr__()

            info_dict['polynomial order'] = self._optimal_polynomial_grade

            info_dict['polynomial fit type'] = self._fit_method_info['bin type']
            info_dict['polynomial fit method'] = self._fit_method_info['fit method']

        return pd.Series(info_dict, index=info_dict.keys())

    def _fit_global_and_determine_optimum_grade(self, cnts, bins, exposure):
        """
        Provides the ability to find the optimum polynomial grade for *binned* counts by fitting the
        total (all channels) to 0-4 order polynomials and then comparing them via a likelihood ratio test.


        :param cnts: counts per bin
        :param bins: the bins used
        :param exposure: exposure per bin
        :return: polynomial grade
        """

        min_grade = 0
        max_grade = 4
        log_likelihoods = []

        for grade in range(min_grade, max_grade + 1):
            polynomial, log_like = polyfit(bins, cnts, grade, exposure)

            log_likelihoods.append(log_like)

        # Found the best one
        delta_loglike = np.array(map(lambda x: 2 * (x[0] - x[1]), zip(log_likelihoods[:-1], log_likelihoods[1:])))

        # print("\ndelta log-likelihoods:")

        # for i in range(max_grade):
        #    print("%s -> %s: delta Log-likelihood = %s" % (i, i + 1, deltaLoglike[i]))

        # print("")

        delta_threshold = 9.0

        mask = (delta_loglike >= delta_threshold)

        if (len(mask.nonzero()[0]) == 0):

            # best grade is zero!
            best_grade = 0

        else:

            best_grade = mask.nonzero()[0][-1] + 1

        return best_grade

    def _unbinned_fit_global_and_determine_optimum_grade(self, events, exposure):
        """
        Provides the ability to find the optimum polynomial grade for *unbinned* events by fitting the
        total (all channels) to 0-4 order polynomials and then comparing them via a likelihood ratio test.


        :param events: an event list
        :param exposure: the exposure per event
        :return: polynomial grade
        """

        # Fit the sum of all the channels to determine the optimal polynomial
        # grade


        min_grade = 0
        max_grade = 4
        log_likelihoods = []

        t_start = self._poly_intervals.start_times
        t_stop = self._poly_intervals.stop_times


        for grade in range(min_grade, max_grade + 1):
            polynomial, log_like = unbinned_polyfit(events, grade, t_start, t_stop, exposure)

            log_likelihoods.append(log_like)

        # Found the best one
        delta_loglike = np.array(map(lambda x: 2 * (x[0] - x[1]), zip(log_likelihoods[:-1], log_likelihoods[1:])))

        delta_threshold = 9.0

        mask = (delta_loglike >= delta_threshold)

        if (len(mask.nonzero()[0]) == 0):

            # best grade is zero!
            best_grade = 0

        else:

            best_grade = mask.nonzero()[0][-1] + 1

        return best_grade

    def _fit_polynomials(self):

        raise NotImplementedError('this must be implemented in a subclass')

    def _unbinned_fit_polynomials(self):

        raise NotImplementedError('this must be implemented in a subclass')

    def save_background(self, filename, overwrite=False):
        """
        save the background to an HD5F

        :param filename:
        :return:
        """

        # make the file name proper

        filename = os.path.splitext(filename)



        filename = "%s.h5" % filename[0]


        filename_sanitized = sanitize_filename(filename)

        # Check that it does not exists
        if os.path.exists(filename_sanitized):

            if overwrite:

                try:

                    os.remove(filename_sanitized)

                except:

                    raise IOError("The file %s already exists and cannot be removed (maybe you do not have "
                                  "permissions to do so?). " % filename_sanitized)

            else:

                raise IOError("The file %s already exists!" % filename_sanitized)

        with HDFStore(filename_sanitized) as store:

            # extract the polynomial information and save it

            if self._poly_fit_exists:

                coeff = []
                err = []

                for poly in self._polynomials:
                    coeff.append(poly.coefficients)
                    err.append(poly.covariance_matrix)
                df_coeff = pd.Series(coeff)
                df_err = pd.Series(err)

            else:

                raise RuntimeError('the polynomials have not been fit yet')

            df_coeff.to_hdf(store, 'coefficients')
            df_err.to_hdf(store, 'covariance')



            store.get_storer('coefficients').attrs.metadata = {'poly_order': self._optimal_polynomial_grade,
                                                               'poly_selections': zip(self._poly_intervals.start_times,self._poly_intervals.stop_times),
                                                               'unbinned':self._unbinned,
                                                               'fit_method':self._fit_method_info['fit method']}

        if self._verbose:

            print("\nSaved fitted background to %s.\n"% filename)

    def restore_fit(self, filename):


        filename_sanitized = sanitize_filename(filename)

        with HDFStore(filename_sanitized) as store:

            coefficients = store['coefficients']



            covariance = store['covariance']

            self._polynomials = []

            # create new polynomials

            for i in range(len(coefficients)):

                coeff = np.array(coefficients.loc[i])

                # make sure we get the right order
                # pandas stores the non-needed coeff
                # as nans.

                coeff = coeff[np.isfinite(coeff)]

                cov  = covariance.loc[i]



                self._polynomials.append(Polynomial.from_previous_fit(coeff, cov))





            metadata = store.get_storer('coefficients').attrs.metadata

            self._optimal_polynomial_grade = metadata['poly_order']
            poly_selections = np.array(metadata['poly_selections'])

            self._poly_intervals = TimeIntervalSet.from_starts_and_stops(poly_selections[:,0],poly_selections[:,1])
            self._unbinned = metadata['unbinned']

            if self._unbinned:
                self._fit_method_info['bin type'] = 'unbinned'

            else:

                self._fit_method_info['bin type'] = 'binned'

            self._fit_method_info['fit method'] = metadata['fit_method']


        # go thru and count the counts!

        self._poly_fit_exists = True

        if self._time_selection_exists:

            self.set_active_time_intervals(*self._time_intervals.to_string().split(','))

    def view_lightcurve(self, start=-10, stop=20., dt=1., use_binner=False):

        raise NotImplementedError('must be implemented in subclass')