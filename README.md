# vwoptimize

Wrapper for Vowpal Wabbit that adds cross-validation, hyper-parameter tuning, more performance metrics, rudimentary text preprocessing and reading from csv/tsv.

In order to search for the best parameters, append question mark after a parameter you would like to tune.

For example, in order to fine-tune learning rate:

    $ vwoptimize.py -d data.vw --learning_rate 0.500? --metric acc

The number of digits after comma controls the precision of the tuner.

If the initial parameter value is written in scientific notation, then search is done in log-space:

    $ vwoptimize.py -d data.vw --l1 1e-07? --metric auc

You can specify all possible values for a parameter with a slash:

    $ vwoptimize.py -d data.vw -b 28 --loss_function squared/hinge/logistic? --ngram 1/2/3? --l1 1e-07? --metric mse

Once the search is done, vwoptimize.py can call vw for you with best found options, if "-f/--final_regressor" is specified:

    $ vwoptimize.py -d data.vw -b 28 --ngram 2/3? -f best_model

The hyper-parameter search is done over 10-fold cross validation (can be controlled with --nfolds and --shuffle).
