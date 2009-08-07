#!/usr/bin/env python

"""
Verifies simple build of a "Hello, world!" program with a static library.
"""

import TestGyp

test = TestGyp.TestGyp()

test.run_gyp('library.gyp', '-Dlibrary=static_library')

test.build_all('library.gyp')

expect = """\
Hello from program.c
Hello from library.c
"""
test.run_built_executable('program', stdout=expect)

test.pass_test()
