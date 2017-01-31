# Copyright 2016 Raytheon BBN Technologies
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0

import asyncio, concurrent
import itertools
import h5py
import pickle
import zlib
import numpy as np
import os.path
import time

from auspex.parameter import Parameter, FilenameParameter
from auspex.stream import DataStreamDescriptor
from auspex.log import logger
from auspex.filters import Filter, InputConnector, OutputConnector


class ElementwiseFilter(Filter):
    """Asynchronously perform elementwise operations on multiple streams:
    e.g. multiply or add all streams element-by-element"""

    sink   = InputConnector()
    source = OutputConnector()

    def __init__(self, **kwargs):
        super(ElementwiseFilter, self).__init__(**kwargs)
        self.sink.max_input_streams = 100
        self.quince_parameters = []

    def operation(self):
        """Must be overridden with the desired mathematical function"""
        pass

    def update_descriptors(self):
        """Must be overridden depending on the desired mathematical function"""
        pass

    async def run(self):
        streams = self.sink.input_streams

        for s in streams[1:]:
            if not np.all(s.descriptor.tuples() == streams[0].descriptor.tuples()):
                raise ValueError("Multiple streams connected to correlator must have matching descriptors.")

        # Buffers for stream data
        stream_data = {s: np.zeros(0, dtype=self.sink.descriptor.dtype) for s in streams}

        # Store whether streams are done
        stream_done = {s: False for s in streams}

        while True:
            # Wait for all of the acquisition to complete
            # Against at least some peoples rational expectations, asyncio.wait doesn't return Futures
            # in the order of the iterable it was passed, but perhaps just in order of completion. So,
            # we construct a dictionary in order that that can be mapped back where we need them:

            futures = {
                asyncio.ensure_future(stream.queue.get()): stream
                for stream in streams
            }

            # Deal with non-equal number of messages using timeout
            responses, pending = await asyncio.wait(futures, return_when=asyncio.FIRST_COMPLETED, timeout=2.0)

            # Construct the inverse lookup, results in {stream: result}
            stream_results = {futures[res]: res.result() for res in list(responses)}

            # Cancel the futures
            for pend in list(pending):
                pend.cancel()

            # Add any new data to the
            for stream, message in stream_results.items():
                message_type = message['type']
                message_data = message['data']
                message_comp = message['compression']
                message_data = pickle.loads(zlib.decompress(message_data)) if message_comp == 'zlib' else message_data
                message_data = message_data if hasattr(message_data, 'size') else np.array([message_data])
                if message_type == 'event' and message_data == 'done':
                    stream_done[stream] = True

                elif message_type == 'data':
                    stream_data[stream] = np.concatenate((stream_data[stream], message_data.flatten()))

            if False not in stream_done.values():
                for oc in self.output_connectors.values():
                    for os in oc.output_streams:
                        await os.push_event("done")
                logger.debug('%s "%s" is done', self.__class__.__name__, self.name)
                break

            # Now process the data with the elementwise operation
            smallest_length = min([d.size for d in stream_data.values()])
            new_data = [d[:smallest_length] for d in stream_data.values()]
            result = new_data[0]
            for nd in new_data[1:]:
                result = self.operation()(result, nd)
            if result.size > 0:
                await self.source.push(result)

            # Add data to carry_data if necessary
            for stream in stream_data.keys():
                if stream_data[stream].size > smallest_length:
                    stream_data[stream] = stream_data[stream][smallest_length:]
                else:
                    stream_data[stream] = np.zeros(0, dtype=self.sink.descriptor.dtype)
