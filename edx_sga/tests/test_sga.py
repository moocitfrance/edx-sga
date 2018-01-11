# -*- coding: utf-8 -*-
"""
Tests for SGA
"""
import datetime
import json
import mimetypes
import unittest
import os
import shutil
import tempfile
import uuid

import mock
import pytest
import pytz
from workbench.runtime import WorkbenchRuntime

try:
    # Python 2
    import __builtin__ as builtins
except ImportError:
    # Python 3
    import builtins

from ddt import ddt, data, unpack  # pylint: disable=import-error
from django.conf import settings  # lint-amnesty, pylint: disable=import-error
from django.contrib.auth.models import User
from django.core.files.storage import FileSystemStorage
from django.utils.timezone import now as django_now  # pylint: disable=import-error
from opaque_keys.edx.locations import Location  # lint-amnesty, pylint: disable=import-error
from opaque_keys.edx.locator import CourseLocator  # lint-amnesty, pylint: disable=import-error
from xblock.field_data import DictFieldData
from xblock.fields import DateTime

from edx_sga.tests.common import DummyResource, dummy_upload


SHA1 = 'da39a3ee5e6b4b0d3255bfef95601890afd80709'
UUID = '8c4b765745f746f7a128470842211601'


pytestmark = pytest.mark.django_db  # pylint: disable=invalid-name


def fake_get_submission(**kwargs):
    """returns fake submission data"""
    answer = {
        "sha1": SHA1,
        "filename": kwargs.get("filename", "file.txt"),
        "mimetype": kwargs.get("mimetype", "mime/type"),
    }
    if kwargs.get("finalized"):
        answer["finalized"] = kwargs.get("finalized")
    return {
        "answer": answer,
        "uuid": UUID,
        "submitted_at": kwargs.get("submitted_at", None)
    }


def fake_upload_submission(upload):
    """returns fake submission data with values calculated from an upload object"""
    return fake_get_submission(
        filename=upload.file.name.encode('utf-8'),
        mimetype=mimetypes.guess_type(upload.file.name.encode('utf-8'))[0]
    )


def fake_student_module():
    """dummy representation of xblock class"""
    return mock.Mock(
        course_id=CourseLocator(org='foo', course='baz', run='bar'),
        module_state_key="foo",
        student=mock.Mock(username="fred6", is_staff=False, password="test"),
        state='{"display_name": "Staff Graded Assignment"}',
        save=mock.Mock()
    )


class FakeWorkbenchRuntime(WorkbenchRuntime):
    """Override for testing purposes"""

    anonymous_student_id = 'MOCK'
    user_is_staff = True

    def __init__(self, *args, **kwargs):
        super(FakeWorkbenchRuntime, self).__init__(*args, **kwargs)

        User.objects.create(username=self.anonymous_student_id)

    def get_real_user(self, username):
        """Get the real user"""
        return User.objects.get(username=username)


@ddt
class StaffGradedAssignmentMockedTests(unittest.TestCase):
    """
    Create a SGA block with mock data.
    """
    def setUp(self):
        """
        Creates a test course ID, mocks the runtime, and creates a fake storage
        engine for use in all tests
        """
        super(StaffGradedAssignmentMockedTests, self).setUp()

        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp))

        # fakes imports
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            """mock imported object if not it is not available"""
            try:
                return real_import(name, *args, **kwargs)
            except ImportError:
                for module in ('courseware', 'lms', 'student', 'xmodule'):
                    if name.startswith("{}.".format(module)) or name == module:
                        return mock.Mock()
                if name == 'safe_lxml':
                    return real_import('lxml', *args, **kwargs)
                raise
        builtins.__import__ = fake_import

        def restore_import():
            """restore builtin importer"""
            builtins.__import__ = real_import

        self.addCleanup(restore_import)

        patcher = mock.patch(
            "edx_sga.sga.default_storage",
            FileSystemStorage(self.tmp))
        patcher.start()
        self.addCleanup(patcher.stop)

        self.course_id = CourseLocator(org='foo', course='baz', run='bar')
        self.runtime = FakeWorkbenchRuntime()
        self.scope_ids = mock.Mock()
        self.staff = mock.Mock(return_value={
            "password": "test",
            "username": "tester",
            "is_staff": True
        })

    def make_xblock(self, display_name=None, **kwargs):
        """
        Creates a XBlock SGA for testing purpose.
        """
        from edx_sga.sga import StaffGradedAssignmentXBlock as cls
        field_data = DictFieldData(kwargs)
        block = cls(self.runtime, field_data, self.scope_ids)
        block.location = Location(
            'foo', 'bar', 'baz', 'category', 'name', 'revision'
        )

        block.xmodule_runtime = self.runtime
        block.course_id = self.course_id
        block.scope_ids.usage_id = "i4x://foo/bar/category/name"
        block.category = 'problem'

        if display_name:
            block.display_name = display_name

        block.start = datetime.datetime(2010, 5, 12, 2, 42, tzinfo=pytz.utc)
        return block

    def test_ctor(self):
        """
        Test points are set correctly.
        """
        block = self.make_xblock(points=10)
        assert block.display_name == "Staff Graded Assignment"
        assert block.points == 10

    def test_max_score(self):
        """
        Text max score is set correctly.
        """
        block = self.make_xblock(points=20)
        assert block.max_score() == 20

    def test_max_score_integer(self):
        """
        Test assigning a float max score is rounded to nearest integer.
        """
        block = self.make_xblock(points=20.4)
        assert block.max_score() == 20

    def personalize_upload(self, block, upload):
        """
        Set values on block from file upload.
        """
        now = datetime.datetime.utcnow().replace(tzinfo=pytz.timezone(getattr(settings, "TIME_ZONE", pytz.utc.zone)))
        block.annotated_mimetype = mimetypes.guess_type(upload.file.name.encode('utf-8'))[0]
        block.annotated_filename = upload.file.name.encode('utf-8')
        block.annotated_sha1 = SHA1
        block.annotated_timestamp = now.strftime(
            DateTime.DATETIME_FORMAT
        )

    @mock.patch('edx_sga.sga._resource', DummyResource)
    @mock.patch('edx_sga.sga.render_template')
    @mock.patch('edx_sga.sga.Fragment')
    def test_student_view(self, fragment, render_template):
        # pylint: disable=unused-argument
        """
        Test student view renders correctly.
        """
        block = self.make_xblock("Custom name")

        with mock.patch(
            'edx_sga.sga.StaffGradedAssignmentXBlock.get_submission',
            return_value={}
        ), mock.patch(
            'edx_sga.sga.StaffGradedAssignmentXBlock.student_state',
            return_value={
                'uploaded': None,
                'annotated': None,
                'upload_allowed': True,
                'max_score': 100,
                'graded': None
            }
        ):
            fragment = block.student_view()
            assert render_template.called is True
            template_arg = render_template.call_args[0][0]
            assert template_arg == 'templates/staff_graded_assignment/show.html'
            context = render_template.call_args[0][1]
            assert context['is_course_staff'] is True
            assert context['id'] == 'name'
            student_state = json.loads(context['student_state'])
            assert student_state['uploaded'] is None
            assert student_state['annotated'] is None
            assert student_state['upload_allowed'] is True
            assert student_state['max_score'] == 100
            assert student_state['graded'] is None
            fragment.add_css.assert_called_once_with(
                DummyResource("static/css/edx_sga.css"))
            fragment.initialize_js.assert_called_once_with(
                "StaffGradedAssignmentXBlock")

    @mock.patch('edx_sga.sga._resource', DummyResource)
    @mock.patch('edx_sga.sga.StaffGradedAssignmentXBlock.upload_allowed')
    @mock.patch('edx_sga.sga.StaffGradedAssignmentXBlock.get_score')
    @mock.patch('edx_sga.sga.render_template')
    @mock.patch('edx_sga.sga.Fragment')
    def test_student_view_with_score(self, fragment, render_template, get_score, upload_allowed):
        # pylint: disable=unused-argument
        """
        Tests scores are displayed correctly on student view.
        """
        block = self.make_xblock()
        get_score.return_value = 10
        upload_allowed.return_value = True
        block.comment = "ok"

        with dummy_upload('foo.txt') as (upload, _):
            with mock.patch(
                'submissions.api.create_submission',
            ) as mocked_create_submission, mock.patch(
                'edx_sga.sga.StaffGradedAssignmentXBlock.student_state', return_value={}
            ):
                block.upload_assignment(mock.Mock(params={'assignment': upload}))
            assert mocked_create_submission.called is True

            with mock.patch(
                'edx_sga.sga.StaffGradedAssignmentXBlock.get_submission',
                return_value=fake_upload_submission(upload)
            ), mock.patch(
                'edx_sga.sga.StaffGradedAssignmentXBlock.student_state',
                return_value={
                    'graded': {'comment': 'ok', 'score': 10},
                    'uploaded': {'filename': 'foo.txt'},
                    'max_score': 100
                }
            ):
                fragment = block.student_view()
                assert render_template.called is True
                template_arg = render_template.call_args[0][0]
                assert template_arg == 'templates/staff_graded_assignment/show.html'
                context = render_template.call_args[0][1]
                assert context['is_course_staff'] is True
                assert context['id'] == 'name'
                student_state = json.loads(context['student_state'])
                assert student_state['uploaded'] == {'filename': 'foo.txt'}
                assert student_state['graded'] == {'comment': 'ok', 'score': 10}
                assert student_state['max_score'] == 100
                fragment.add_css.assert_called_once_with(
                    DummyResource("static/css/edx_sga.css"))
                fragment.initialize_js.assert_called_once_with(
                    "StaffGradedAssignmentXBlock")

    def test_studio_view(self):
        # pylint: disable=unused-argument
        """
        Test studio view is using the StudioEditableXBlockMixin function
        """
        with mock.patch('edx_sga.sga.StudioEditableXBlockMixin.studio_view') as studio_view_mock:
            block = self.make_xblock()
            block.studio_view()
        studio_view_mock.assert_called_once_with(None)

    def test_save_sga(self):
        """
        Tests save SGA  block on studio.
        """
        def weights_positive_float_test():
            """
            tests weight is non negative float.
            """
            orig_weight = 11.0

            # Test negative weight doesn't work
            block.save_sga(mock.Mock(method="POST", body=json.dumps({
                "display_name": "Test Block",
                "points": '100',
                "weight": -10.0})))
            assert block.weight == orig_weight

            # Test string weight doesn't work
            block.save_sga(mock.Mock(method="POST", body=json.dumps({
                "display_name": "Test Block",
                "points": '100',
                "weight": "a"})))
            assert block.weight == orig_weight

        def point_positive_int_test():
            """
            Tests point is positive number.
            """
            # Test negative doesn't work
            block.save_sga(mock.Mock(method="POST", body=json.dumps({
                "display_name": "Test Block",
                "points": '-10',
                "weight": 11})))
            assert block.points == orig_score

            # Test float doesn't work
            block.save_sga(mock.Mock(method="POST", body=json.dumps({
                "display_name": "Test Block",
                "points": '24.5',
                "weight": 11})))
            assert block.points == orig_score

        orig_score = 23
        block = self.make_xblock()
        block.save_sga(mock.Mock(body='{}'))
        assert block.display_name == "Staff Graded Assignment"
        assert block.points == 100
        assert block.weight is None
        block.save_sga(mock.Mock(method="POST", body=json.dumps({
            "display_name": "Test Block",
            "points": str(orig_score),
            "weight": 11})))
        assert block.display_name == "Test Block"
        assert block.points == orig_score
        assert block.weight == 11

        point_positive_int_test()
        weights_positive_float_test()

    @mock.patch('edx_sga.sga.StaffGradedAssignmentXBlock.get_student_item_dict')
    @mock.patch('edx_sga.sga.StaffGradedAssignmentXBlock.upload_allowed')
    @mock.patch('edx_sga.sga._get_sha1')
    def test_upload_download_assignment(self, _get_sha1, upload_allowed, get_student_item_dict):
        """
        Tests upload and download assignment for non staff.
        """
        file_name = 'test.txt'
        block = self.make_xblock()
        get_student_item_dict.return_value = {
            "student_id": 1,
            "course_id": block.block_course_id,
            "item_id": block.block_id,
            "item_type": 'sga',
        }
        upload_allowed.return_value = True

        with dummy_upload(file_name) as (upload, expected):
            with mock.patch('submissions.api.create_submission') as mocked_create_submission, mock.patch(
                "edx_sga.sga.StaffGradedAssignmentXBlock.file_storage_path",
                return_value=block.file_storage_path(SHA1, file_name)
            ), mock.patch(
                'edx_sga.sga.StaffGradedAssignmentXBlock.student_state', return_value={}
            ):
                block.upload_assignment(mock.Mock(params={'assignment': upload}))
            assert mocked_create_submission.called is True

            with mock.patch(
                'edx_sga.sga.StaffGradedAssignmentXBlock.get_submission',
                return_value=fake_upload_submission(upload)
            ), mock.patch(
                "edx_sga.sga.StaffGradedAssignmentXBlock.file_storage_path",
                return_value=block.file_storage_path(SHA1, file_name)
            ):
                response = block.download_assignment(None)
                assert response.body == expected

            with mock.patch(
                "edx_sga.sga.StaffGradedAssignmentXBlock.file_storage_path",
                return_value=block.file_storage_path("", "test_notfound.txt")
            ), mock.patch(
                'edx_sga.sga.StaffGradedAssignmentXBlock.get_submission',
                return_value=fake_upload_submission(upload)
            ):
                response = block.download_assignment(None)
                assert response.status_code == 404

    @mock.patch('edx_sga.sga.StaffGradedAssignmentXBlock.get_student_item_dict')
    @mock.patch('edx_sga.sga.StaffGradedAssignmentXBlock.upload_allowed')
    @data(({'finalized': False}, True), ({}, True), ({'finalized': True}, False))
    @unpack
    def test_finalize_uploaded_assignment(
            self, finalized_setting, model_change_expected, upload_allowed, get_student_item_dict
    ):
        """
        Tests that finalize_uploaded_assignment sets a submission to be finalized
        """
        block = self.make_xblock()
        get_student_item_dict.return_value = {
            "student_id": 1,
            "course_id": block.block_course_id,
            "item_id": block.block_id,
            "item_type": 'sga',
        }
        upload_allowed.return_value = True
        existing_submitted_at_value = django_now()
        fake_submission_data = fake_get_submission(**finalized_setting)
        fake_submission_object = mock.Mock(
            submitted_at=existing_submitted_at_value,
            answer=fake_submission_data['answer']
        )

        with mock.patch(
            'edx_sga.sga.Submission.objects.get', return_value=fake_submission_object
        ), mock.patch(
            'edx_sga.sga.StaffGradedAssignmentXBlock.get_submission', return_value=fake_submission_data
        ), mock.patch(
            'edx_sga.sga.StaffGradedAssignmentXBlock.student_state', return_value={}
        ):
            block.finalize_uploaded_assignment(mock.Mock())

        assert fake_submission_object.answer['finalized'] is True
        assert (existing_submitted_at_value != fake_submission_object.submitted_at) is model_change_expected
        assert fake_submission_object.save.called is model_change_expected

    @mock.patch('edx_sga.sga.StaffGradedAssignmentXBlock.get_module_by_id')
    @mock.patch('edx_sga.sga.StaffGradedAssignmentXBlock.is_course_staff')
    @mock.patch('edx_sga.sga._get_sha1')
    def test_staff_upload_download_annotated(self, _get_sha1, is_course_staff, get_module_by_id):
        # pylint: disable=no-member
        """
        Tests upload and download of annotated staff files.
        """
        get_module_by_id.return_value = fake_student_module()
        is_course_staff.return_value = True
        _get_sha1.return_value = SHA1
        file_name = 'test.txt'
        block = self.make_xblock()

        with dummy_upload(file_name) as (upload, expected), mock.patch(
            "edx_sga.sga.StaffGradedAssignmentXBlock.staff_grading_data",
            return_value={}
        ) as staff_grading_data:
            block.staff_upload_annotated(mock.Mock(params={'annotated': upload, 'module_id': 1}))
        assert staff_grading_data.called is True

        with mock.patch(
            "edx_sga.sga.StaffGradedAssignmentXBlock.file_storage_path",
            return_value=block.file_storage_path(SHA1, file_name)
        ):
            response = block.staff_download_annotated(mock.Mock(params={'module_id': 1}))
            assert response.body == expected

        with mock.patch(
            "edx_sga.sga.StaffGradedAssignmentXBlock.file_storage_path",
            return_value=block.file_storage_path("", "test_notfound.txt")
        ):
            response = block.staff_download_annotated(
                mock.Mock(params={'module_id': 1})
            )
            assert response.status_code == 404

    @mock.patch('edx_sga.sga.StaffGradedAssignmentXBlock.get_module_by_id')
    @mock.patch('edx_sga.sga.StaffGradedAssignmentXBlock.is_course_staff')
    @mock.patch('edx_sga.sga._get_sha1')
    def test_download_annotated(self, _get_sha1, is_course_staff, get_module_by_id):
        # pylint: disable=no-member
        """
        Test download annotated assignment for non staff.
        """
        get_module_by_id.return_value = fake_student_module()
        is_course_staff.return_value = True
        _get_sha1.return_value = SHA1

        file_name = 'test.txt'
        block = self.make_xblock()

        with dummy_upload(file_name) as (upload, expected):
            with mock.patch(
                "edx_sga.sga.StaffGradedAssignmentXBlock.staff_grading_data",
                return_value={}
            ) as staff_grading_data:
                block.staff_upload_annotated(mock.Mock(params={
                    'annotated': upload,
                    'module_id': 1
                }))
            assert staff_grading_data.called is True
            self.personalize_upload(block, upload)

        with mock.patch(
            "edx_sga.sga.StaffGradedAssignmentXBlock.file_storage_path",
            return_value=block.file_storage_path(SHA1, file_name)
        ):
            response = block.download_annotated(None)
            assert response.body == expected

        with mock.patch(
            "edx_sga.sga.StaffGradedAssignmentXBlock.file_storage_path",
            return_value=block.file_storage_path("", "test_notfound.txt")
        ):
            response = block.download_annotated(None)
            assert response.status_code == 404

    @mock.patch('edx_sga.sga.StaffGradedAssignmentXBlock.upload_allowed')
    @mock.patch('edx_sga.sga.StaffGradedAssignmentXBlock.get_module_by_id')
    @mock.patch('edx_sga.sga.StaffGradedAssignmentXBlock.is_course_staff')
    @mock.patch('edx_sga.sga._get_sha1')
    def test_staff_download(self, _get_sha1, is_course_staff, get_module_by_id, upload_allowed):
        """
        Test download for staff.
        """
        get_module_by_id.return_value = fake_student_module()
        is_course_staff.return_value = True
        upload_allowed.return_value = True
        _get_sha1.return_value = SHA1
        block = self.make_xblock()

        with dummy_upload('test.txt') as (upload, expected), mock.patch(
            'edx_sga.sga.StaffGradedAssignmentXBlock.student_state', return_value={}
        ), mock.patch("submissions.api.create_submission") as mocked_create_submission:
            block.upload_assignment(mock.Mock(params={'assignment': upload}))
        assert mocked_create_submission.called is True
        self.personalize_upload(block, upload)

        with mock.patch(
            'edx_sga.sga.StaffGradedAssignmentXBlock.get_submission',
            return_value=fake_upload_submission(upload)
        ):
            response = block.staff_download(mock.Mock(params={
                'student_id': 1}))
            assert response.body == expected

        with mock.patch(
            "edx_sga.sga.StaffGradedAssignmentXBlock.file_storage_path",
            return_value=block.file_storage_path("", "test_notfound.txt")
        ), mock.patch(
            'edx_sga.sga.StaffGradedAssignmentXBlock.get_submission',
            return_value=fake_upload_submission(upload)
        ):
            response = block.staff_download(
                mock.Mock(params={'student_id': 1})
            )
            assert response.status_code == 404

    @unpack
    @data(
        {'past_due': False, 'score': None, 'is_finalized_submission': False, 'expected_value': True},
        {'past_due': True, 'score': None, 'is_finalized_submission': False, 'expected_value': False},
        {'past_due': False, 'score': 80, 'is_finalized_submission': False, 'expected_value': False},
        {'past_due': False, 'score': None, 'is_finalized_submission': True, 'expected_value': False},
    )
    def test_upload_allowed(self, past_due, score, is_finalized_submission, expected_value):
        """
        Tests that upload_allowed returns the right value under certain conditions
        """
        block = self.make_xblock()
        with mock.patch(
            "edx_sga.sga.StaffGradedAssignmentXBlock.past_due",
            return_value=past_due
        ), mock.patch(
            "edx_sga.sga.StaffGradedAssignmentXBlock.get_score",
            return_value=score
        ), mock.patch(
            "edx_sga.sga.is_finalized_submission",
            return_value=is_finalized_submission
        ):
            assert block.upload_allowed(submission_data={}) is expected_value

    @mock.patch('edx_sga.sga.zip_student_submissions')
    @mock.patch('edx_sga.sga.StaffGradedAssignmentXBlock.get_sorted_submissions')
    @data((False, False), (True, True))
    @unpack
    def test_prepare_download_submissions(
            self,
            is_zip_file_available,
            downloadable,
            get_sorted_submissions,
            zip_student_submissions,
    ):
        """
        Test prepare download api
        """
        block = self.make_xblock()
        get_sorted_submissions.return_value = [
            {
                'submission_id': uuid.uuid4().hex,
                'filename': "test_{}.txt".format(uuid.uuid4().hex),
                'timestamp': datetime.datetime.now(tz=pytz.utc)
            } for __ in range(2)
        ]
        zip_student_submissions.delay = mock.Mock()
        with mock.patch(
            "edx_sga.sga.StaffGradedAssignmentXBlock.is_zip_file_available",
            return_value=is_zip_file_available
        ), mock.patch(
            'edx_sga.sga.StaffGradedAssignmentXBlock.get_real_user',
            return_value=self.staff
        ), mock.patch(
            'edx_sga.utils.default_storage.modified_time',
            return_value=datetime.datetime.now()
        ):
            response = block.prepare_download_submissions(None)
            response_body = json.loads(response.body)
            assert response_body["downloadable"] is downloadable

    @mock.patch('edx_sga.sga.zip_student_submissions')
    @mock.patch('edx_sga.sga.StaffGradedAssignmentXBlock.get_sorted_submissions')
    def test_prepare_download_submissions_task_called(
            self,
            get_sorted_submissions,
            zip_student_submissions
    ):
        """
        Test prepare download api
        """
        block = self.make_xblock()
        get_sorted_submissions.return_value = [
            {
                'submission_id': uuid.uuid4().hex,
                'filename': "test_{}.txt".format(uuid.uuid4().hex),
                'timestamp': datetime.datetime.utcnow()
            } for __ in range(2)
        ]
        zip_student_submissions.delay = mock.Mock()
        with mock.patch(
            "edx_sga.sga.StaffGradedAssignmentXBlock.is_zip_file_available",
            return_value=False
        ), mock.patch(
            'edx_sga.sga.StaffGradedAssignmentXBlock.get_real_user',
            return_value=self.staff
        ), mock.patch(
            'edx_sga.sga.default_storage.modified_time',
            return_value=datetime.datetime.now()
        ):
            response = block.prepare_download_submissions(None)
            response_body = json.loads(response.body)
            assert response_body["downloadable"] is False

        zip_student_submissions.delay.assert_called_with(
            unicode(block.block_course_id),
            unicode(block.block_id),
            unicode(block.location),
            self.staff.username
        )

    @data((False, False), (True, True))
    @unpack
    def test_download_submissions_status(self, is_zip_file_available, downloadable):
        """test download_submissions_status api"""
        block = self.make_xblock()
        with mock.patch(
            "edx_sga.sga.StaffGradedAssignmentXBlock.is_zip_file_available",
            return_value=is_zip_file_available
        ):
            response = block.download_submissions_status(None)
            response_body = json.loads(response.body)
            assert response_body["zip_available"] is downloadable

    @mock.patch('edx_sga.sga.StaffGradedAssignmentXBlock.is_course_staff')
    def test_download_submissions(self, is_course_staff):
        """tests download_submissions"""
        block = self.make_xblock()
        is_course_staff.return_value = True

        expected = b"some information"
        filename = "foo.zip"
        path = os.path.join(self.tmp, filename)
        with open(path, "wb") as temp_file:
            temp_file.write(expected)

        with mock.patch(
            "edx_sga.sga.get_zip_file_path", return_value=path
        ), mock.patch(
            'edx_sga.sga.StaffGradedAssignmentXBlock.get_real_user',
            return_value=self.staff
        ), mock.patch(
            "edx_sga.sga.get_zip_file_name", return_value=filename
        ):
            response = block.download_submissions(None)
            assert response.status_code == 200
            assert response.body == expected
