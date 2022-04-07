import collections
import faulthandler
import functools
import gc
import importlib
import io
import os
import sys
import time
import traceback
import unittest

from test import support
from test.libregrtest.refleak import dash_R, clear_caches
from test.libregrtest.save_env import saved_test_environment
from test.libregrtest.utils import format_duration, print_warning

# This must match the same global in test_sbs_stdlib. Unfortunately there isn't
# a place to put this which can be imported by both users without either
# disrupting tests with unexpected imports, or being unavailable in some of our
# CI setups.
N_SBS_TEST_CLASSES = 10

# Test result constants.
PASSED = 1
FAILED = 0
ENV_CHANGED = -1
SKIPPED = -2
RESOURCE_DENIED = -3
INTERRUPTED = -4
CHILD_ERROR = -5   # error in a child process
TEST_DID_NOT_RUN = -6
TIMEOUT = -7

_FORMAT_TEST_RESULT = {
    PASSED: '%s passed',
    FAILED: '%s failed',
    ENV_CHANGED: '%s failed (env changed)',
    SKIPPED: '%s skipped',
    RESOURCE_DENIED: '%s skipped (resource denied)',
    INTERRUPTED: '%s interrupted',
    CHILD_ERROR: '%s crashed',
    TEST_DID_NOT_RUN: '%s run no tests',
    TIMEOUT: '%s timed out',
}

# Minimum duration of a test to display its duration or to mention that
# the test is running in background
PROGRESS_MIN_TIME = 30.0   # seconds

# small set of tests to determine if we have a basically functioning interpreter
# (i.e. if any of these fail, then anything else is likely to follow)
STDTESTS = [
    'test_grammar',
    'test_opcodes',
    'test_dict',
    'test_builtin',
    'test_exceptions',
    'test_types',
    'test_unittest',
    'test_doctest',
    'test_doctest2',
    'test_support'
]

# set of tests that we don't want to be executed when using regrtest
NOTTESTS = set()

# If these test directories are encountered recurse into them and treat each
# test_ .py or dir as a separate test module. This can increase parallelism.
# Beware this can't generally be done for any directory with sub-tests as the
# __init__.py may do things which alter what tests are to be run.
SPLITTESTDIRS = {
    "test_asyncio",
    "test_compiler",
}

# Remap named tests to other names during `findtests()`. This is useful for
# translating "virtual"/generated tests into multiple named tests so they can be
# run in parallel.
REMAP_TESTS = {
    "test_sbs_stdlib": [
        f"test_compiler.test_sbs_stdlib.SbsCompileTests{i}"
        for i in range(N_SBS_TEST_CLASSES)
    ]
}

# used by --findleaks, store for gc.garbage
FOUND_GARBAGE = []


def is_failed(result, ns):
    ok = result.result
    if ok in (PASSED, RESOURCE_DENIED, SKIPPED, TEST_DID_NOT_RUN):
        return False
    if ok == ENV_CHANGED:
        return ns.fail_env_changed
    return True


def format_test_result(result):
    fmt = _FORMAT_TEST_RESULT.get(result.result, "%s")
    text = fmt % result.test_name
    if result.result == TIMEOUT:
        text = '%s (%s)' % (text, format_duration(result.test_time))
    return text


def findtestdir(path=None):
    return path or os.path.dirname(os.path.dirname(__file__)) or os.curdir


def findtests(
    testdir=None,
    stdtests=STDTESTS,
    nottests=NOTTESTS,
    splittestdirs=SPLITTESTDIRS,
    base_mod="",
):
    """Return a list of all applicable test modules."""
    testdir = findtestdir(testdir)
    names = os.listdir(testdir)
    tests = []
    others = set(stdtests) | nottests
    for name in names:
        mod, ext = os.path.splitext(name)
        if mod in REMAP_TESTS.keys():
            tests.extend(REMAP_TESTS[mod])
        elif mod[:5] == "test_" and mod not in others:
            if mod in splittestdirs:
                subdir = os.path.join(testdir, mod)
                if len(base_mod):
                    mod = f"{base_mod}.{mod}"
                else:
                    mod = f"test.{mod}"
                tests.extend(findtests(subdir, [], nottests, splittestdirs, mod))
            elif ext in (".py", ""):
                tests.append(f"{base_mod}.{mod}" if len(base_mod) else mod)
    return stdtests + sorted(tests)


def get_abs_module(ns, test_name):
    if test_name.startswith('test.') or ns.testdir:
        return test_name
    else:
        # Import it from the test package
        return 'test.' + test_name


TestResult = collections.namedtuple('TestResult',
    'test_name result test_time xml_data')

def _runtest(ns, test_name):
    # Handle faulthandler timeout, capture stdout+stderr, XML serialization
    # and measure time.

    output_on_failure = ns.verbose3

    use_timeout = (ns.timeout is not None)
    if use_timeout:
        faulthandler.dump_traceback_later(ns.timeout, exit=True)

    start_time = time.perf_counter()
    try:
        support.set_match_tests(ns.match_tests, ns.ignore_tests)
        support.junit_xml_list = xml_list = [] if ns.xmlpath else None
        if ns.failfast:
            support.failfast = True

        if output_on_failure:
            support.verbose = True

            stream = io.StringIO()
            orig_stdout = sys.stdout
            orig_stderr = sys.stderr
            try:
                sys.stdout = stream
                sys.stderr = stream
                result = _runtest_inner(ns, test_name,
                                        display_failure=False)
                if result != PASSED:
                    output = stream.getvalue()
                    orig_stderr.write(output)
                    orig_stderr.flush()
            finally:
                sys.stdout = orig_stdout
                sys.stderr = orig_stderr
        else:
            # Tell tests to be moderately quiet
            support.verbose = ns.verbose

            result = _runtest_inner(ns, test_name,
                                    display_failure=not ns.verbose)

        if xml_list:
            import xml.etree.ElementTree as ET
            xml_data = [ET.tostring(x).decode('us-ascii') for x in xml_list]
        else:
            xml_data = None

        test_time = time.perf_counter() - start_time

        return TestResult(test_name, result, test_time, xml_data)
    finally:
        if use_timeout:
            faulthandler.cancel_dump_traceback_later()
        support.junit_xml_list = None


def runtest(ns, test_name):
    """Run a single test.

    ns -- regrtest namespace of options
    test_name -- the name of the test

    Returns the tuple (result, test_time, xml_data), where result is one
    of the constants:

        INTERRUPTED      KeyboardInterrupt
        RESOURCE_DENIED  test skipped because resource denied
        SKIPPED          test skipped for some other reason
        ENV_CHANGED      test failed because it changed the execution environment
        FAILED           test failed
        PASSED           test passed
        EMPTY_TEST_SUITE test ran no subtests.
        TIMEOUT          test timed out.

    If ns.xmlpath is not None, xml_data is a list containing each
    generated testsuite element.
    """
    try:
        return _runtest(ns, test_name)
    except:
        if not ns.pgo:
            msg = traceback.format_exc()
            print(f"test {test_name} crashed -- {msg}",
                  file=sys.stderr, flush=True)
        return TestResult(test_name, FAILED, 0.0, None)


def _test_module(the_module):
    loader = unittest.TestLoader()
    tests = loader.loadTestsFromModule(the_module)
    for error in loader.errors:
        print(error, file=sys.stderr)
    if loader.errors:
        raise Exception("errors while loading tests")
    support.run_unittest(tests)


def _runtest_inner2(ns, test_name):
    # Load the test function, run the test function, handle huntrleaks
    # and findleaks to detect leaks

    abstest = get_abs_module(ns, test_name)

    # remove the module from sys.module to reload it if it was already imported
    support.unload(abstest)

    try:
        the_module = importlib.import_module(abstest)

        # If the test has a test_main, that will run the appropriate
        # tests.  If not, use normal unittest test loading.
        test_runner = getattr(the_module, "test_main", None)
        if test_runner is None:
            test_runner = functools.partial(_test_module, the_module)
    except ModuleNotFoundError:

        def test_runner():
            loader = unittest.TestLoader()
            tests = loader.loadTestsFromName(abstest)
            for error in loader.errors:
                print(error, file=sys.stderr)
            if loader.errors:
                raise Exception("errors while loading tests")
            support.run_unittest(tests)

    try:
        if ns.huntrleaks:
            # Return True if the test leaked references
            refleak = dash_R(ns, test_name, test_runner)
        else:
            test_runner()
            refleak = False
    finally:
        cleanup_test_droppings(test_name, ns.verbose)

    support.gc_collect()

    if gc.garbage:
        support.environment_altered = True
        print_warning(f"{test_name} created {len(gc.garbage)} "
                      f"uncollectable object(s).")

        # move the uncollectable objects somewhere,
        # so we don't see them again
        FOUND_GARBAGE.extend(gc.garbage)
        gc.garbage.clear()

    support.reap_children()

    return refleak


def _runtest_inner(ns, test_name, display_failure=True):
    # Detect environment changes, handle exceptions.

    # Reset the environment_altered flag to detect if a test altered
    # the environment
    support.environment_altered = False

    if ns.pgo:
        display_failure = False

    try:
        clear_caches()

        with saved_test_environment(test_name, ns.verbose, ns.quiet, pgo=ns.pgo) as environment:
            refleak = _runtest_inner2(ns, test_name)
    except support.ResourceDenied as msg:
        if not ns.quiet and not ns.pgo:
            print(f"{test_name} skipped -- {msg}", flush=True)
        return RESOURCE_DENIED
    except unittest.SkipTest as msg:
        if not ns.quiet and not ns.pgo:
            print(f"{test_name} skipped -- {msg}", flush=True)
        return SKIPPED
    except support.TestFailed as exc:
        msg = f"test {test_name} failed"
        if display_failure:
            msg = f"{msg} -- {exc}"
        print(msg, file=sys.stderr, flush=True)
        return FAILED
    except support.TestDidNotRun:
        return TEST_DID_NOT_RUN
    except KeyboardInterrupt:
        print()
        return INTERRUPTED
    except:
        if not ns.pgo:
            msg = traceback.format_exc()
            print(f"test {test_name} crashed -- {msg}",
                  file=sys.stderr, flush=True)
        return FAILED

    if refleak:
        return FAILED
    if environment.changed:
        return ENV_CHANGED
    return PASSED


def cleanup_test_droppings(test_name, verbose):
    # First kill any dangling references to open files etc.
    # This can also issue some ResourceWarnings which would otherwise get
    # triggered during the following test run, and possibly produce failures.
    support.gc_collect()

    # Try to clean up junk commonly left behind.  While tests shouldn't leave
    # any files or directories behind, when a test fails that can be tedious
    # for it to arrange.  The consequences can be especially nasty on Windows,
    # since if a test leaves a file open, it cannot be deleted by name (while
    # there's nothing we can do about that here either, we can display the
    # name of the offending test, which is a real help).
    for name in (support.TESTFN,):
        if not os.path.exists(name):
            continue

        if os.path.isdir(name):
            import shutil
            kind, nuker = "directory", shutil.rmtree
        elif os.path.isfile(name):
            kind, nuker = "file", os.unlink
        else:
            raise RuntimeError(f"os.path says {name!r} exists but is neither "
                               f"directory nor file")

        if verbose:
            print_warning(f"{test_name} left behind {kind} {name!r}")
            support.environment_altered = True

        try:
            import stat
            # fix possible permissions problems that might prevent cleanup
            os.chmod(name, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
            nuker(name)
        except Exception as exc:
            print_warning(f"{test_name} left behind {kind} {name!r} "
                          f"and it couldn't be removed: {exc}")
