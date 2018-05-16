import pytest

from pypi import wheeltool


def test_parse_metadata():
    wheel_path = 'pypi/testdata/attrs-18.1.0-py2.py3-none-any.whl'
    wheel = wheeltool.Wheel(wheel_path)
    assert list(wheel.dependencies()) == []
    assert set(wheel.dependencies(extra='tests')) == set([
        'coverage',
        'hypothesis',
        'pympler',
        'pytest',
        'six',
        'zope.interface',
    ])
    assert set(wheel.dependencies(extra='dev')) == set([
        'coverage',
        'hypothesis',
        'pympler',
        'pytest',
        'six',
        'zope.interface',
        'sphinx',
    ])
    assert set(wheel.dependencies(extra='docs')) == set(
        ['sphinx', 'zope.interface'])
    assert set(wheel.extras()) == set(['dev', 'tests', 'docs'])


if __name__ == '__main__':
    pytest.main()
