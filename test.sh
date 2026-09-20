#!/usr/bin/env bash
#
# Run all pyspider unit test scripts, one file at a time.
#
# Usage:
#   ./test.sh                          run all unit tests under tests/
#   ./test.sh tests/test_database.py   run specific test file(s)
#
# Environment variables:
#   PYTHON        python interpreter to use (default: python)
#   PYTEST_OPTS   extra options passed to pytest (default: -v)
#   IGNORE_ALL=1      skip tests that need external services
#   IGNORE_MYSQL=1    skip mysql tests
#   IGNORE_MONGODB=1  skip mongodb tests
#   IGNORE_REDIS=1    skip redis tests
#   IGNORE_RABBITMQ=1 skip rabbitmq tests
#   IGNORE_ELASTICSEARCH=1 skip elasticsearch tests
#   IGNORE_PHANTOMJS=1   skip phantomjs tests
#   IGNORE_SPLASH=1      skip splash tests
#   IGNORE_PUPPETEER=1   skip puppeteer tests
#
# Exit code is non-zero if any test file fails.

set -u

cd "$(dirname "$0")"

PYTHON=${PYTHON:-python}
PYTEST_OPTS=${PYTEST_OPTS:--v}

if [ $# -gt 0 ]; then
    TEST_FILES="$@"
else
    TEST_FILES=$(ls tests/test_*.py | sort)
fi

PASSED=""
FAILED=""

for test_file in $TEST_FILES; do
    echo "======================================================================"
    echo "Running $test_file"
    echo "======================================================================"
    if "$PYTHON" -m pytest $PYTEST_OPTS "$test_file"; then
        PASSED="$PASSED $test_file"
    else
        FAILED="$FAILED $test_file"
    fi
    echo
done

echo "======================================================================"
echo "Test summary"
echo "======================================================================"
for test_file in $PASSED; do
    echo "PASS: $test_file"
done
for test_file in $FAILED; do
    echo "FAIL: $test_file"
done

if [ -n "$FAILED" ]; then
    echo
    echo "Some test files FAILED."
    exit 1
fi
echo
echo "All test files passed."
exit 0
