#!/usr/bin/python
# Copyright 2017 The Bazel Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Modified version of rules_python's wheel tool. Original version:

https://github.com/bazelbuild/rules_python
"""

import collections
import json
import os
import re
import rfc822
import sys
import zipfile

import pkg_resources
from pkg_resources._vendor.packaging import markers


def recurse_split_extra(parsed_parts):
  extra = ''
  remaining = []

  i = 0
  for part in parsed_parts:
    if isinstance(part, list):
      # parenthesized expressions are lists
      sub_extra, sub_remaining = recurse_split_extra(part)
      if sub_extra != '':
        assert extra == ''
        extra = sub_extra
      remaining.append(sub_remaining)
    elif isinstance(part, tuple):
      if isinstance(part[0], markers.Variable) and part[0].value == 'extra':
        # Found the extra part: parse it and skip it
        op = part[1]
        value = part[2]
        assert isinstance(op, markers.Op) and op.value == '=='
        assert isinstance(value, markers.Value)
        assert len(value.value) > 0
        assert extra == ''
        extra = value.value

        # if the previous item is now a dangling boolean operator: trim it
        if len(remaining) > 0 and isinstance(remaining[-1], basestring):
          remaining = remaining[:-1]
      else:
        remaining.append(part)
    elif isinstance(part, basestring):
      # must be an operator: just append it
      remaining.append(part)
    else:
      raise Exception('unhandled part: ' + repr(part))

  # if the first item is a dangling boolean operator: trim it
  if len(remaining) > 0 and isinstance(remaining[0], basestring):
    remaining = remaining[1:]
  return extra, remaining

def recurse_str(parsed_parts):
  out = ''
  for part in parsed_parts:
    if isinstance(part, list):
      out += '(' + recurse_str(part) + ')'
    elif isinstance(part, tuple):
      out += ' '.join(p.serialize() for p in part)
    elif isinstance(part, basestring):
      out += ' ' + part + ' '
    else:
      raise Exception('unhandled part: ' + repr(part))
  return out


def split_extra_from_environment_marker(environment_marker):
  '''
  Splits an environment marker into (extra, remaining environment). It parses the expression,
  then finds the "extra==X" clause. That clause is removed, and the expression is serialized.
  '''

  marker = markers.Marker(environment_marker)
  extra, remaining = recurse_split_extra(marker._markers)

  # rebuild the string
  environment_string = recurse_str(remaining)

  return extra, environment_string


class Wheel(object):
    def __init__(self, path):
        self._path = path

    def path(self):
        return self._path

    def basename(self):
        return os.path.basename(self.path())

    def distribution(self):
        # See https://www.python.org/dev/peps/pep-0427/#file-name-convention
        parts = self.basename().split('-')
        return parts[0]

    def version(self):
        # See https://www.python.org/dev/peps/pep-0427/#file-name-convention
        parts = self.basename().split('-')
        return parts[1]

    def repository_name(self):
        # Returns the canonical name of the Bazel repository for this package.
        canonical = 'pypi__{}_{}'.format(self.distribution(), self.version())
        # Escape any illegal characters with underscore.
        return re.sub('[-.]', '_', canonical)

    def _dist_info(self):
        # Return the name of the dist-info directory within the .whl file.
        # e.g. google_cloud-0.27.0-py2.py3-none-any.whl ->
        #      google_cloud-0.27.0.dist-info
        return '{}-{}.dist-info'.format(self.distribution(), self.version())

    def metadata(self):
        # Extract the structured data from metadata.json in the WHL's dist-info
        # directory.
        with zipfile.ZipFile(self.path(), 'r') as whl:
            # first check for metadata.json
            try:
                with whl.open(
                        os.path.join(self._dist_info(), 'metadata.json')) as f:
                    return json.loads(f.read().decode("utf-8"))
            except KeyError:
                pass
            # fall back to METADATA file (https://www.python.org/dev/peps/pep-0427/)
            with whl.open(os.path.join(self._dist_info(), 'METADATA')) as f:
                return self._parse_metadata(f.read().decode("utf-8"))

    def name(self):
        return self.metadata().get('name')

    def dependencies(self, extra=None):
        """Access the dependencies of this Wheel.

        Args:
          extra: if specified, include the additional dependencies
                of the named "extra".

        Yields:
          the names of requirements from the metadata.json
        """
        # TODO(mattmoor): Is there a schema to follow for this?
        run_requires = self.metadata().get('run_requires', [])
        for requirement in run_requires:
            if requirement.get('extra') != extra:
                # Match the requirements for the extra we're looking for.
                continue
            marker = requirement.get('environment')
            if marker and not pkg_resources.evaluate_marker(marker):
                # The current environment does not match the provided PEP 508 marker,
                # so ignore this requirement.
                continue
            requires = requirement.get('requires', [])
            for entry in requires:
                # Strip off any trailing versioning data.
                parts = re.split('[ ><=()]', entry)
                yield parts[0]

    def extras(self):
        return self.metadata().get('extras', [])

    def expand(self, directory):
        with zipfile.ZipFile(self.path(), 'r') as whl:
            whl.extractall(directory)

    def _parse_metadata(self, content):
        """Parse `METADATA` files into the `metadata.json` format.

        Parses according to https://www.python.org/dev/peps/pep-0314/.
        """
        requirement_pattern = re.compile('Requires-Dist: (.*)')
        name_pattern = re.compile('Name: (.*)')
        extra_pattern = re.compile(' *extra *== *\'([^ ;\']*)\'')

        parsed = {}

        name_data = name_pattern.search(content).group(1)
        parsed['name'] = name_data

        raw_requirements = requirement_pattern.findall(content)
        main_requirements = []
        extra_to_requirements = collections.defaultdict(list)
        for raw_requirement in raw_requirements:
            # There are three patterns I've seen for 'Requires-Dist' lines:
            #   1. Requires-Dist: some_package
            #   2. Requires-Dist: some_package; extra == "some_extra"
            #   3. Requires-Dist: some_package; python_version > 3.2"
            # We attempt to handle the first two, and are ignoring the last one
            # for now.
            semicolon_pos = raw_requirement.find(';')
            if semicolon_pos == -1:
                # No extra listed. Append to main requirements.
                main_requirements.append(raw_requirement.strip())
            else:
                # There may be an extra. Search for one.
                extra_requirement = raw_requirement[:semicolon_pos].strip()
                raw_extra = raw_requirement[semicolon_pos + 1:]
                extra_match = extra_pattern.match(raw_extra)
                # If `extra_match` is `None`, then it's probably a
                # `python_version` specification, which we currently ignore.
                # TODO(josh): Handle `python_version` as well.
                if extra_match is not None:
                    extra = extra_match.group(1)
                    extra_to_requirements[extra].append(extra_requirement)
        parsed['run_requires'] = [{
            'requires': main_requirements,
        }] + [{
            'extra': extra,
            'requires': requirements
        } for extra, requirements in extra_to_requirements.items()]

        parsed['extras'] = list(extra_to_requirements)

        return parsed


def main():
    if len(sys.argv) != 2:
        sys.stderr.write('Usage: wheeltool.py (input wheel)\n')
        sys.exit(1)
    wheel = Wheel(sys.argv[1])

    extra_deps = {}
    for extra in wheel.extras():
        extra_deps[extra] = list(wheel.dependencies(extra=extra))

    output = dict(
        requires=list(wheel.dependencies()),
        extras=extra_deps,
    )
    print(json.dumps(output))


if __name__ == '__main__':
  main()
