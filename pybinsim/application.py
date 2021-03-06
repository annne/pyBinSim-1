# This file is part of the pyBinSim project.
#
# Copyright (c) 2017 A. Neidhardt, F. Klein, N. Knoop, T. Köllmer
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

""" Module contains main loop and configuration of pyBinSim """
import time

import numpy as np
import pyaudio

from pybinsim.convolver import ConvolverFFTW
from pybinsim.filterstorage import FilterStorage
from pybinsim.osc_receiver import OscReceiver
from pybinsim.soundhandler import SoundHandler


class BinSimConfig(object):
    def __init__(self):

        # Default Configuration
        self.configurationDict = {'soundfile': '',
                                  'blockSize': 256,
                                  'filterSize': 16384,
                                  'filterList': 'brirs/filter_list_kemar5.txt',
                                  'enableCrossfading': 'False',
                                  'useHeadphoneFilter': 'False',
                                  'loudnessFactor': float(1),
                                  'maxChannels': 8,
                                  'samplingRate': 44100}

    def read_from_file(self, filepath):
        config = open(filepath, 'r')

        for line in config:
            line_content = str.split(line)
            key = line_content[0]
            if key in self.configurationDict:
                self.configurationDict[key] = type(self.configurationDict[key])(line_content[1])
            else:
                print('Entry ' + key + ' is unknown')

    def get(self, setting):
        return self.configurationDict[setting]


class BinSim(object):
    """
    Main pyBinSim program logic
    """

    def __init__(self, config_file):
        print("BinSim: init")

        # Read Configuration File
        self.config = BinSimConfig()
        self.config.read_from_file(config_file)

        self.current_config = self.config
        self.nChannels = self.current_config.get('maxChannels')
        self.sampleRate = self.current_config.get('samplingRate')
        self.blockSize = self.current_config.get('blockSize')

        self.result = None
        self.block = None
        self.stream = None

        self.convolverHP, self.convolvers, self.filterStorage, self.oscReceiver, self.soundHandler = self.initialize_pybinsim()

        self.p = pyaudio.PyAudio()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.__cleanup()

    def stream_start(self):
        print("BinSim: stream_start")
        self.stream = self.p.open(format=pyaudio.paFloat32, channels=2,
                                  rate=self.sampleRate, output=True,
                                  frames_per_buffer=self.blockSize,
                                  stream_callback=audio_callback(self))
        self.stream.start_stream()

        while self.stream.is_active():
            time.sleep(1)

    def initialize_pybinsim(self):

        self.result = np.empty([self.config.get('blockSize'), 2], np.dtype(np.float32))
        self.block = np.empty([self.config.get('maxChannels'), self.config.get('blockSize')], np.dtype(np.float32))

        # Create FilterStorage
        print(type(self.config.get('blockSize')))
        filterStorage = FilterStorage(self.config.get('filterSize'), self.config.get('blockSize'),
                                      self.config.get('filterList'))

        # Start an oscReceiver
        oscReceiver = OscReceiver()
        oscReceiver.start_listening()
        time.sleep(1)

        # Create SoundHandler
        soundHandler = SoundHandler(self.config.get('blockSize'), self.config.get('maxChannels'),
                                    self.config.get('samplingRate'))
        soundHandler.request_new_sound_file([self.config.get('soundfile')])

        # Create N convolvers depending on the number of wav channels
        print('Number of Channels: ' + str(self.config.get('maxChannels')))
        convolvers = [None] * self.config.get('maxChannels')
        for n in range(self.config.get('maxChannels')):
            convolvers[n] = ConvolverFFTW(self.config.get('filterSize'), self.config.get('blockSize'), False)

        # HP Equalization convolver
        convolverHP = None
        if self.config.get('useHeadphoneFilter') == 'True':
            convolverHP = ConvolverFFTW(self.config.get('filterSize'), self.config.get('blockSize'), True)
            left, right = filterStorage.get_filter(['HPFILTER'])
            convolverHP.setIR(left, right, False)

        return convolverHP, convolvers, filterStorage, oscReceiver, soundHandler

    def close(self):
        print("BinSim: close")
        self.stream_close()
        self.p.terminate()

    def stream_close(self):
        print("BinSim: stream_close")
        self.stream.stop_stream()
        self.stream.close()

    def __cleanup(self):
        # Close everything when BinSim is finished
        self.filterStorage.close()
        self.close()

        self.oscReceiver.close()

        for n in range(self.config.get('maxChannels')):
            self.convolvers[n].close()

        if self.config.get('useHeadphoneFilter') == 'True':
            if self.convolverHP:
                self.convolverHP.close()


def audio_callback(binsim):
    """ Wrapper for callback to hand over custom data """
    assert isinstance(binsim, BinSim)

    # The pyAudio Callback
    def callback(in_data, frame_count, time_info, status):
        # print("pyAudio callback")

        current_soundfile_list = binsim.oscReceiver.get_sound_file_list()
        if current_soundfile_list:
            binsim.soundHandler.request_new_sound_file(current_soundfile_list)

        # Get sound block. At least one convolver should exist
        binsim.block[:binsim.soundHandler.get_sound_channels(), :] = binsim.soundHandler.buffer_read()

        # Update Filters and run each convolver with the current block
        for n in range(binsim.soundHandler.get_sound_channels()):

            # Get new Filter
            if binsim.oscReceiver.is_filter_update_necessary(n):
                # print('Updating Filter')
                filterValueList = binsim.oscReceiver.get_current_values(n)
                leftFilter, rightFilter = binsim.filterStorage.get_filter(filterValueList)
                binsim.convolvers[n].setIR(leftFilter, rightFilter, callback.config.get('enableCrossfading'))

            left, right = binsim.convolvers[n].process(binsim.block[n, :])

            # Sum results from all convolvers
            if n == 0:
                binsim.result[:, 0] = left
                binsim.result[:, 1] = right
            else:
                binsim.result[:, 0] += left
                binsim.result[:, 1] += right

        # Finally apply Headphone Filter
        if callback.config.get('useHeadphoneFilter') == 'True':
            binsim.result[:, 0], binsim.result[:, 1] = binsim.convolverHP.process(binsim.result)

        # Scale data
        binsim.result *= 1 / float((callback.config.get('maxChannels') + 1) * 2)
        binsim.result *= callback.config.get('loudnessFactor')

        # When the last block is small than the blockSize, this is probably the end of the file.
        # Call pyaudio to stop after this frame
        if binsim.block.size < callback.config.get('blockSize'):
            pyaudio.paContinue = 1

        return (binsim.result[:frame_count].tostring(), pyaudio.paContinue)

    callback.config = binsim.config

    return callback
