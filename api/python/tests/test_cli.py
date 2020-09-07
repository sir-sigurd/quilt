import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

import pytest

import quilt3
from quilt3 import main

from .utils import QuiltTestCase

create_parser = main.create_parser


class CommandLineTestCase(QuiltTestCase):
    """
    Base TestCase class, sets up a CLI parser
    """
    @classmethod
    def setUpClass(cls):
        parser = create_parser()
        cls.parser = parser


class QuiltCLITestCase(CommandLineTestCase):
    def test_quilt_config(self):
        args = self.parser.parse_args(['config', 'https://foo.bar'])
        assert args.catalog_url == 'https://foo.bar'

    def test_push(self):
        name = 'test/name'
        pkg = quilt3.Package()

        with tempfile.TemporaryDirectory() as tmp_dir:
            (Path(tmp_dir) / 'foo').touch()
            (Path(tmp_dir) / 'bar').mkdir()
            (Path(tmp_dir) / 'bar' / 'baz')

            with mock.patch('quilt3.Package.__new__', return_value=pkg) as mocked_package_class, \
                 mock.patch.object(pkg, 'set_dir', wraps=pkg.set_dir) as mocked_set_dir, \
                 mock.patch.object(pkg, 'push') as mocked_push:
                main.main(('push', '--dir', tmp_dir, name))

                mocked_package_class.assert_called_once_with(quilt3.Package)
                mocked_set_dir.assert_called_once_with('.', tmp_dir, meta=None)
                mocked_push.assert_called_once_with(name, registry=None, dest=None, message=None)

    def test_install(self):
        name = 'asah/gpt3'
        with mock.patch('quilt3.Package.install') as mocked_package_install:
            main.main(('install', name, '--registry', 's3://some-bucket'))

            mocked_package_install.assert_called_once_with(
                name,
                dest=None,
                dest_registry=None,
                path=None,
                registry='s3://some-bucket',
                top_hash=None
            )

        with mock.patch('quilt3.Package.install') as mocked_package_install:
            config_file = Path(__file__).parent / 'data/quilt.yml'
            shutil.copy(config_file, 'quilt.yml')

            main.main(('install', 'quilt.yml'))

            assert mocked_package_install.call_count == 5
            # remove created file
            os.remove("quilt.yml")


@pytest.mark.parametrize(
    'meta_arg, meta_data, expected_set_dir_count, expected_push_count, expected_meta, expected_stderr',
    [
        (None, None, 1, 1, None, ''),
        ('--meta', '{invalid: meta}', 0, 0, {}, 'is not a valid json string'),
        ('--meta', "{'single': 'quotation'}", 0, 0, {}, 'is not a valid json string'),
        ('--meta', '{"test": "meta", }', 0, 0, {}, 'is not a valid json string'),
        ('--meta', '{"test": "meta"}', 1, 1, {"test": "meta"}, ''),
    ]
)
def test_push_with_meta_data(
    meta_arg,
    meta_data,
    expected_set_dir_count,
    expected_push_count,
    expected_meta,
    expected_stderr,
    capsys
):
    name = 'test/name'
    pkg = quilt3.Package()

    with tempfile.TemporaryDirectory() as tmp_dir:
        (Path(tmp_dir) / 'foo').touch()
        (Path(tmp_dir) / 'bar').mkdir()
        (Path(tmp_dir) / 'bar' / 'baz')

        with mock.patch('quilt3.Package.__new__', return_value=pkg) as mocked_package_class,\
             mock.patch.object(pkg, 'set_dir', wraps=pkg.set_dir) as mocked_set_dir, \
             mock.patch.object(pkg, 'push') as mocked_push:

            # '--registry' defaults to configured remote registry hence optional.
            if meta_arg:
                main.main(('push', '--dir', tmp_dir, name, meta_arg, meta_data))
            else:
                main.main(('push', '--dir', tmp_dir, name))
            mocked_package_class.assert_called_once_with(quilt3.Package)
            assert mocked_set_dir.call_count == expected_set_dir_count
            assert mocked_push.call_count == expected_push_count
            assert pkg.meta == expected_meta
            # check for expected stderr exception message
            captured = capsys.readouterr()
            assert expected_stderr in captured.err
