from typing import Callable, Dict, Generator, List, Optional
from functools import wraps
from dataclasses import dataclass
import time
import logging

import numpy as np

from qm.qua import *
try:
    from qm.QuantumMachinesManager import QuantumMachinesManager
except:
    from qm.quantum_machines_manager import QuantumMachinesManager

from labcore.measurement import *
from labcore.measurement.record import make_data_spec
from labcore.measurement.sweep import AsyncRecord

from .config import QMConfig

# --- Options that need to be set by the user for the OPX to work ---
# config object that when called returns the config dictionary as expected by the OPX
config: Optional[QMConfig] = None  # OPX config dictionary
qmachine_mgr = None # Quantum machine manager
qmachine = None # Quantum machine


logger = logging.getLogger(__name__)


class QuantumMachineContext:
    """
    Context manager for the Quantum Machine. It will open the machine when entering the context and close it when
    exiting, after all measurement completed. This is used via a with statement, i.e.:

    ```
    with QuantumMachineContext() as qmc:
        [your measurement code here]
    ```

    This does not need to be used, but if measurements are done repeatedly, it saves some time.

    Warning: Using a context manager doesn't let you update the config of the OPX. If you want to change the
    config, you need to open a new quantum machine.
    """
    def __enter__(self):
        global qmachine_mgr, qmachine, config
        qmachine_mgr = QuantumMachinesManager(
                host=config.opx_address,
                port=config.opx_port, 
                cluster_name=config.cluster_name,
                octave=config.octave
            )
        qmachine = qmachine_mgr.open_qm(config(), close_other_machines=False)
        return qmachine

    def __exit__(self, exc_type, exc_value, traceback):
        global qmachine, qmachine_mgr, config
        if qmachine is not None:
            qmachine.close()
            qmachine = None
            qmachine_mgr = None


@dataclass
class TimedOPXData(DataSpec):
    def __post_init__(self):
        super().__post_init__()
        if self.depends_on is None or len(self.depends_on) == 0:
            deps = []
        else:
            deps = list(self.depends_on)
        self.depends_on = [self.name+'_time_points'] + deps

@dataclass
class ComplexOPXData(DataSpec):
    i_data_stream: str = 'I'
    q_data_stream: str = 'Q'


class RecordOPXdata(AsyncRecord):
    """
    Implementation of AsyncRecord for use with the OPX machine.
    """

    def __init__(self, *specs):
        self.communicator = {}
        # self.communicator['raw_variables'] = []
        self.user_data = []
        self.specs = []
        for s in specs:
            spec = make_data_spec(s)
            self.specs.append(spec)
            if isinstance(spec, TimedOPXData):
                tspec = indep(spec.name + "_time_points")
                self.specs.append(tspec)
                self.user_data.append(tspec.name)

    def setup(self, fun, *args, **kwargs) -> None:
        """
        Establishes connection with the OPX and starts the measurement. The config of the OPX is passed through
        the module variable global_config. It saves the result handles and saves initial values to the communicator
        dictionary.
        """
        global qmachine, qmachine_mgr
        self.communicator["self_managed"] = False
        # Start the measurement in the OPX.
        if qmachine_mgr is None and qmachine is None:
            qmachine_mgr = QuantumMachinesManager(
                host=config.opx_address,
                port=config.opx_port, 
                cluster_name=config.cluster_name,
                octave=config.octave
            )

            qmachine = qmachine_mgr.open_qm(config(), close_other_machines=False)
            logger.info(f"current QM: {qmachine}, {qmachine.id}")
            self.communicator["self_managed"] = True

        job = qmachine.execute(fun(*args, **kwargs))
        result_handles = job.result_handles

        # Save the result handle and create initial parameters in the communicator used in the collector.
        self.communicator['result_handles'] = result_handles
        self.communicator['active'] = True
        self.communicator['counter'] = 0
        self.communicator['manager'] = qmachine_mgr
        self.communicator['qmachine'] = qmachine
        self.communicator['qmachine_id'] = qmachine.id

    # FIXME change this such that we make sure that we have enough data on all handles
    def _wait_for_data(self, batchsize: int) -> None:
        """
        Waits for the opx to have measured more data points than the ones indicated in the batchsize. Also checks that
        the OPX is still collecting data, when the OPX is no longer processing, turn communicator['active'] to False to
        exhaust the collector.

        :param batchsize: Size of batch. How many data-points is the minimum for the sweep to get in an iteration.
                          e.g. if 5, _control_progress will keep running until at least 5 new data-points
                          are available for collection.
        """

        # When ready becomes True, the infinite loop stops.
        ready = False

        # Collect necessary values from communicator.
        res_handle = self.communicator['result_handles']
        counter = self.communicator['counter']

        while not ready:
            statuses = []
            processing = []
            for name, handle in res_handle:
                current_datapoint = handle.count_so_far()

                # Check if the OPX is still processing.
                if res_handle.is_processing():
                    processing.append(True)

                    # Check if enough data-points are available.
                    if current_datapoint - counter >= batchsize:
                        statuses.append(True)
                    else:
                        statuses.append(False)

                else:
                    # Once the OPX is done processing turn ready True and turn active False to exhaust the generator.
                    statuses.append(True)
                    processing.append(False)

            if not False in statuses:
                ready = True
            if not True in processing:
                self.communicator['active'] = False

    def cleanup(self):
        """
        Functions in charge of cleaning up any software tools that needs cleanup.

        Currently, manually closes the qmachine in the OPT so that simultaneous measurements can occur.
        """
        global qmachine, qmachine_mgr
        logger.info('Cleaning up')

        open_machines = qmachine_mgr.list_open_quantum_machines()
        logger.info(f"currently open QMs: {open_machines}")
        if self.communicator["self_managed"]:
            machine_id = qmachine.id
            qmachine.close()
            logger.info(f"QM with ID {machine_id} closed.")

            qmachine = None
            qmachine_mgr = None
        


    def collect(self, batchsize: int = 100) -> Generator[Dict, None, None]:
        """
        Implementation of collector for the OPX. Collects new data-points from the OPX and yields them in a dictionary
        with the names of the recorded variables as keywords and numpy arrays with the values. Raises ValueError if a
        stream name inside the QUA program has a different name than a recorded variable and if the amount of recorded
        variables and streams are different.

        :param batchsize: Size of batch. How many data-points is the minimum for the sweep to get in an iteration.
                          e.g. if 5, _control_progress will keep running until at least 5 new data-points
                          are available for collection.
        """

        # Get the result_handles from the communicator.
        result_handle = self.communicator['result_handles']
        try:
            while self.communicator['active']:
                # Restart values for each iteration.
                return_data = {}
                counter = self.communicator['counter']  # Previous iteration data-point number.
                first = True
                available_points = 0
                ds: Optional[DataSpec] = None

                # Make sure that the result_handle is active.
                if result_handle is None:
                    yield None

                # Waits until new data-points are ready to be gathered.
                self._wait_for_data(batchsize)

                def get_data_from_handle(name, up_to):
                    if up_to == counter:
                        return None
                    handle = result_handle.get(name)
                    handle.wait_for_values(up_to)
                    data = np.squeeze(handle.fetch(slice(counter, up_to))['value'])
                    return data

                for i, ds in enumerate(self.specs):
                    if isinstance(ds, ComplexOPXData):
                        iname = ds.i_data_stream
                        qname = ds.q_data_stream
                        if i == 0:
                            available_points = result_handle.get(iname).count_so_far()
                        idata = get_data_from_handle(iname, up_to=available_points)
                        qdata = get_data_from_handle(qname, up_to=available_points)
                        if (qdata is None or idata is None):
                            print(f'qdata is: {qdata}')
                            print(f'idata is: {idata}')
                            print(f'available points is:{available_points}')
                            print(f'i is: {i}')
                            print(f'ds is: {ds}')
                            print(f'iname is: {iname}')
                            print(f'qname is: {qdata}')
                            print(f'am I active: {self.communicator["active"]}')
                            print(f'counter is: {self.communicator["counter"]}')

                        if qdata is not None and idata is not None:
                            return_data[ds.name] = idata + 1j*qdata

                    elif ds.name in self.user_data:
                        continue

                    elif ds.name not in result_handle:
                        raise RuntimeError(f'{ds.name} specified but cannot be found in result handle.')

                    else:
                        name = ds.name
                        if i == 0:
                            available_points = result_handle.get(name).count_so_far()
                        return_data[name] = get_data_from_handle(name, up_to=available_points)

                    if isinstance(ds, TimedOPXData):
                        data = return_data[ds.name]
                        if data is not None:
                            tvals = np.arange(1, data.shape[-1]+1)
                            if len(data.shape) == 1:
                                return_data[name + '_time_points'] = tvals
                            elif len(data.shape) == 2:
                                return_data[name + '_time_points'] = np.tile(tvals, data.shape[0]).reshape(data.shape[0], -1)
                            else:
                                raise NotImplementedError('someone needs to look at data saving ASAP...')

                self.communicator['counter'] = available_points
                yield return_data

        finally:
            self.cleanup()
