#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Deal with the scriptworker version in semver format.

Copied from scriptharness.

However, since writing this I've discovered that setuptools and sphinx don't
accept all semver formatted versions.  It's not clear if this will go away.

When called as a script, this will update ../version.json with the appropriate
version info.

Attributes:
  __version__ (Tuple[int, int, int, str]): semver version - three integers and an
    optional string.
  __version_string__ (str): semver version in string format.

"""
from __future__ import absolute_import, division, print_function, \
                       unicode_literals
import json
import os


# get_version_string {{{1
def get_version_string(version):
    """Translate a version tuple into a string.

    Specify the __version__ as a tuple for more precise comparisons, and
    translate it to __version_string__ for when that's needed.

    This function exists primarily for easier unit testing.

    Args:
      version (Tuple[int, int, int, str]): three ints and an optional string.

    Returns:
      version_string (str): the tuple translated into a string per PEP 440

    """
    version_len = len(version)
    if version_len == 3:
        version_string = '%d.%d.%d' % version
    elif version_len == 4:
        version_string = '%d.%d.%d%s' % version
    else:
        raise Exception(
            'Version tuple is non-semver-compliant {} length!'.format(version_len)
        )
    return version_string


# 1}}}
# Semantic versioning 2.0.0  http://semver.org/
__version__ = (27, 1, 0, "dev4")
__version_string__ = get_version_string(__version__)


# write_version {{{1
def write_version(name=None, path=None):
    """Write the version info to ../version.json, for setup.py.

    Args:
      name (Optional[str]): this is for the ``write_version(name=__name__)``
        below.  That's one way to both follow the
        ``if __name__ == '__main__':`` convention but also allow for full
        coverage without ignoring parts of the file.

      path (Optional[str]): the path to write the version json to.  Defaults
        to ../version.json
    """
    # Written like this for coverage purposes.
    # http://stackoverflow.com/questions/5850268/how-to-test-or-mock-if-name-main-contents/27084447#27084447
    if name in (None, '__main__'):
        path = path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "version.json")
        contents = {
            'version': __version__,
            'version_string': __version_string__,
        }
        with open(path, 'w') as filehandle:
            print(json.dumps(contents, sort_keys=True, indent=4, separators=(',', ':')), file=filehandle)


write_version(name=__name__)
