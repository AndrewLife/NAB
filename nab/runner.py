# ----------------------------------------------------------------------
# Copyright (C) 2014-2015, Numenta, Inc.  Unless you have an agreement
# with Numenta, Inc., for a separate license for this software code, the
# following terms and conditions apply:
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see http://www.gnu.org/licenses.
#
# http://numenta.org/licenses/
# ----------------------------------------------------------------------

import multiprocessing
import os
import pandas
try:
  import simplejson as json
except ImportError:
  import json

from nab.corpus import Corpus
from nab.detectors.base import detectDataSet
from nab.labeler import CorpusLabel
from nab.optimizer import optimizeThreshold
from nab.scorer import scoreCorpus
from nab.util import updateThresholds



class Runner(object):
  """
  Class to run an endpoint (detect, optimize, or score) on the NAB
  benchmark using the specified set of profiles, thresholds, and/or detectors.
  """

  def __init__(self,
               dataDir,
               resultsDir,
               labelPath,
               profilesPath,
               thresholdPath,
               numCPUs=None):
    """
    @param dataDir        (string)  Directory where all the raw datasets exist.

    @param resultsDir     (string)  Directory where the detector anomaly scores
                                    will be scored.

    @param labelPath      (string)  Path where the labels of the datasets
                                    exist.

    @param profilesPath   (string)  Path to JSON file containing application
                                    profiles and associated cost matrices.

    @param thresholdPath  (string)  Path to thresholds dictionary containing the
                                    best thresholds (and their corresponding
                                    score) for a combination of detector and
                                    user profile.

    @probationaryPercent  (float)   Percent of each dataset which will be
                                    ignored during the scoring process.

    @param numCPUs        (int)     Number of CPUs to be used for calls to
                                    multiprocessing.pool.map
    """
    self.dataDir = dataDir
    self.resultsDir = resultsDir

    self.labelPath = labelPath
    self.profilesPath = profilesPath
    self.thresholdPath = thresholdPath
    self.pool = multiprocessing.Pool(numCPUs)
    
    self.probationaryPercent = 0.15
    self.windowSize = 0.10

    self.corpus = None
    self.corpusLabel = None
    self.profiles = None


  def initialize(self):
    """Initialize all the relevant objects for the run."""
    self.corpus = Corpus(self.dataDir)
    self.corpusLabel = CorpusLabel(path=self.labelPath, corpus=self.corpus)

    with open(self.profilesPath) as p:
      self.profiles = json.load(p)


  def detect(self, detectors):
    """Generate results file given a dictionary of detector classes

    Function that takes a set of detectors and a corpus of data and creates a
    set of files storing the alerts and anomaly scores given by the detectors

    @param detectors     (dict)         Dictionary with key value pairs of a
                                        detector name and its corresponding
                                        class constructor.
    """
    print "\nRunning detection step"
    
    count = 0
    args = []
    for detectorName, detectorConstructor in detectors.iteritems():
      for relativePath, dataSet in self.corpus.dataFiles.iteritems():

        args.append(
          (
            count,
            detectorConstructor(
              dataSet=dataSet,
              probationaryPercent=self.probationaryPercent),
            detectorName,
            self.corpusLabel.labels[relativePath]["label"],
            self.resultsDir,
            relativePath
          )
        )

        count += 1
    
    self.pool.map(detectDataSet, args)


  def optimize(self, detectorNames):
    """Optimize the threshold for each combination of detector and profile.

    @param detectorNames  (list)  List of detector names.

    @return thresholds    (dict)  Dictionary of dictionaries with detector names
                                  then profile names as keys followed by another
                                  dictionary containing the score and the
                                  threshold used to obtained that score.
    """
    print "\nRunning optimize step"
    
    scoreFlag = False
    thresholds = {}

    for detectorName in detectorNames:
      resultsDetectorDir = os.path.join(self.resultsDir, detectorName)
      resultsCorpus = Corpus(resultsDetectorDir)

      thresholds[detectorName] = {}

      for profileName, profile in self.profiles.iteritems():
        thresholds[detectorName][profileName] = optimizeThreshold(
          (self.pool,
           detectorName,
           profileName,
           profile["CostMatrix"],
           resultsDetectorDir,
           resultsCorpus,
           self.corpusLabel,
           self.probationaryPercent,
           scoreFlag))

    updateThresholds(thresholds, self.thresholdPath)

    return thresholds


  def score(self, detectorNames, thresholds):
    """Score the performance of the detectors.

    Function that must be called only after detection result files have been
    generated and thresholds have been optimized. This looks at the result files
    and scores the performance of each detector specified and stores these
    results in a csv file.

    @param detectorNames  (list)    List of detector names.

    @param thresholds     (dict)    Dictionary of dictionaries with detector
                                    names then profile names as keys followed by
                                    another dictionary containing the score and
                                    the threshold used to obtained that score.
    """
    print "\nRunning scoring step"
    
    scoreFlag = True

    self.resultsFiles = []
    for detectorName in detectorNames:
      resultsDetectorDir = os.path.join(self.resultsDir, detectorName)
      resultsCorpus = Corpus(resultsDetectorDir)

      for profileName, profile in self.profiles.iteritems():
      
        threshold = thresholds[detectorName][profileName]["threshold"]
        resultsDF = scoreCorpus(threshold,
                                (self.pool,
                                 detectorName,
                                 profileName,
                                 profile["CostMatrix"],
                                 resultsDetectorDir,
                                 resultsCorpus,
                                 self.corpusLabel,
                                 self.probationaryPercent,
                                 scoreFlag))

        scorePath = os.path.join(resultsDetectorDir, "%s_%s_scores.csv" %\
          (detectorName, profileName))
        resultsDF.to_csv(scorePath, index=False)
        print "%s detector benchmark scores written to %s" %\
          (detectorName, scorePath)
        self.resultsFiles.append(scorePath)


  def normalize(self):
    """Normalize the detectors' scores according to the Baseline.
    
    Function can only be called with the scoring step (i.e. runner.score())
    preceding it.
    This reads the total score values from the results CSVs, and
    adds the relevant baseline value.
    """
    print "\nRunning score normalization step"

    # Get baselines for each application profile
    baselineDir = os.path.join(self.resultsDir, "baseline")
    if not os.path.isdir(baselineDir):
      raise IOError("No results directory for baseline. You must "
        "run the baseline detector before normalizing scores.")
    baselines = {}
    for profileName, _ in self.profiles.iteritems():
      fileName = baselineDir + "/baseline_" + profileName + "_scores.csv"
      with open(fileName) as f:
        results = pandas.read_csv(f)
        baselines[profileName] = results["Score"][len(results)-1]
    
    for fileName in self.resultsFiles:
      csvName = fileName.split("/")[-1]
      base = baselines[csvName.split("_")[1]]
      with open(fileName) as f:
        results = pandas.read_csv(f)
        score = -base + results["Score"][len(results)-1]
      
      print "Final score for \'%s\' = %.2f" % (csvName[:-11], score)
      