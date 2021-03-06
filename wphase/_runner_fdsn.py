import sys, os, traceback
import cPickle as pickle
from itertools import izip

from obspy.core.utcdatetime import UTCDateTime
from obspy.clients.fdsn import Client
from obspy.core.inventory import Inventory
from wphase.psi.inventory import GetData, Build_metadata_dict
from wphase.psi.core import wpinv, WPInvWarning

import wphase.settings as settings
from wphase.wputils import OutputDict, WPInvProfiler, post_process_wpinv



def load_metadata(
        server_url,
        eqinfo,
        dist_range,
        networks,
        t_before_origin=3600.,
        t_after_origin=3600.):

    """
    :param float t_after_origin: The end of the time window around
        *eqinfo['time']* (in seconds) in which the station must exist. This
        should not be confused with the parameter *t_afterWP* used elsewhere,
        which is used in a more complicated calculation which requires the
        location of the station, which we get in this function. This is only
        used to filter the request for the inventory and hence only need be
        very rough (within the week would probably be equally sensible).
    """

    client = Client(server_url)

    def caller_maker(depth=0, **kwargs):
        if 'network' in kwargs and kwargs['network'].upper() == 'ALL':
            kwargs.pop('network')

        base_call = {
            "level"    : 'response',
            "channel"  : 'BH?',
            "latitude" : eqinfo['lat'],
            "longitude": eqinfo['lon'],
            "minradius": dist_range[0],
            "maxradius": dist_range[1],
            "starttime": eqinfo['time'] - t_before_origin,
            "endtime"  : eqinfo['time'] + t_after_origin}

        base_call.update(kwargs)

        def make_call(**kwargs):
            args = base_call.copy()
            args.update(kwargs)
            return client.get_stations(**args)

        if depth == 0:
            def caller():
                return make_call()
        elif depth == 1:
            def caller(net):
                return make_call(network=net)
        elif depth == 2:
            def caller(net, sta):
                return make_call(network=net, station=sta)
        elif depth == 3:
            def caller(net, sta, cha):
                return make_call(network=net, station=sta, channel=cha)

        return caller

    try:
        # first, try and get everything
        inv = caller_maker(network=networks)()

    except:
        # ... that didn't work
        nets = caller_maker(network=networks, level='network')()
        inv = Inventory([], None)

        # try by network
        call1 = caller_maker(1)
        for net in nets:
            try:
                inv += call1(net.code)
            except:
                # ... by station
                stas = caller_maker(network=net.code, level='station')()
                call2 = caller_maker(2)
                for sta in stas[0]:
                    try:
                        inv += call2(net.code, sta.code)
                    except:
                        # ... by channel
                        chans = caller_maker(network=net.code, station=sta.code, level='channel')()
                        call3 = caller_maker(3)
                        for chan in chans[0][0]:
                            try:
                                inv += call3(net.code, sta.code, chan.code)
                            except:
                                # ... skip the channel
                                # TODO: log that this has happenned
                                pass

    return (server_url,) + Build_metadata_dict(inv)



def runwphase(
        output_dir,
        server,
        greens_functions_dir = settings.GREEN_DIR,
        n_workers_in_pool = settings.N_WORKERS_IN_POOL,
        processing_level = 3,
        stations_to_exclude = None,
        output_dir_can_exist = False,
        networks = 'II,IU',
        eqinfo = None,
        wp_tw_factor = 15,
        t_beforeP = 1500.,
        t_afterWP = 60.,
        dist_range = [5.,90.],
        add_ptime = True,
        bulk_chunk_len = 200,
        prune_cutoffs = None,
        use_only_z_components = True,
        inventory = None,
        **kwargs):

    """
    Run wphase.

    :param output_dir: Full file path to the output directory. **DO NOT USE
        RELATIVE PATHS**.
    :param greens_functions_dir: The Green data Directory.
    :param n_workers_in_pool: Number of processors to use, (default
        :py:data:`wphase.settings.N_WORKERS_IN_POOL`) specifies as many as is
        reasonable'.
    :param processing_level: Processing level.
    :param stations_to_exclude: List of station identifiers to exclude.
    :param output_dir_can_exist: Can the output directory already exist?
    """

    streams = None
    meta_t_p = {}
    streams_pickle_file = os.path.join(output_dir, 'streams.pkl')

    if eqinfo is None:
        raise ValueError('eqinfo cannot be None')

    # get the metadata for the server
    if streams is None:
        print 'creating metadata dict'
        if inventory is not None:
            if server is None:
                raise TypeError('server cannot be None if inventory is not None')
            if isinstance(server, list):
                if not isinstance(inventory, list):
                    raise TypeError('inventory must be a list if server is a list')
                if len(server) != len(inventory):
                    raise ValueError('inventory must be the same length server')
                metas = [Build_metadata_dict(i) for i in inventory]
                servers = server
            elif isinstance(server, basestring):
                metas = [Build_metadata_dict(inventory)]
                servers = [server]
            else:
                raise TypeError('server must be instance of list or basestring')

        elif server.lower() == 'antelope':
            raise Exception("Antelope is no longer supported.")

        # get the metadata for the event
        server, metadata, failures = load_metadata(
                server,
                eqinfo,
                dist_range,
                networks)

        if failures:
            with open(os.path.join(output_dir, 'inv.errs'), 'w') as err_out:
                err_out.write('\n'.join(failures))

        if metadata:
            with open(os.path.join(output_dir, 'inv.pkl'), 'w') as inv_out:
                pickle.dump(metadata, inv_out)
        else:
            raise Exception('no metadata avaialable for: \n{}'.format(
                '\n\t'.join('{}: {}'.format(*kv) for kv in eqinfo.iteritems())))

        servers = [server]
        metas = [metadata]

    wphase_output = OutputDict()

    try:
        if streams is None:
            for server, metadata in izip(servers, metas):
                print 'fetching data from {}'.format(server)
                # load the data for from the appropriate server
                streams_, meta_t_p_ = GetData(
                    eqinfo,
                    metadata,
                    wp_tw_factor = wp_tw_factor,
                    t_beforeP = t_beforeP,
                    t_afterWP = t_afterWP,
                    server = server,
                    dist_range = dist_range,
                    add_ptime = add_ptime,
                    bulk_chunk_len = bulk_chunk_len,
                    prune_cutoffs = prune_cutoffs)

                if use_only_z_components:
                    streams_ = streams_.select(component = 'Z')

                if streams is None:
                    streams = streams_
                else:
                    streams += streams_

                meta_t_p.update(meta_t_p_)

            with open(streams_pickle_file, 'w') as pkle:
                pickle.dump((meta_t_p, streams), pkle)

        # do and post-process the inversion
        with WPInvProfiler(wphase_output, output_dir):
            inv = wpinv(
                streams,
                meta_t_p,
                eqinfo,
                greens_functions_dir,
                processes = n_workers_in_pool,
                OL = processing_level,
                output_dic = wphase_output)

            post_process_wpinv(
                res = inv,
                wphase_output = wphase_output,
                WPOL = processing_level,
                working_dir = output_dir,
                eqinfo = eqinfo,
                metadata = meta_t_p)

    except WPInvWarning, e:
        wphase_output.add_warning(str(e))
    except Exception, e:
        wphase_output[settings.WPHASE_ERROR_KEY] = str(e)
        wphase_output[settings.WPHASE_ERROR_STACKTRACE_KEY] = "".join(traceback.format_exception(*sys.exc_info()))

    return wphase_output
