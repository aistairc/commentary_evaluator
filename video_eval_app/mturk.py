from icecream import ic

import re
from datetime import datetime
import json
import copy

from django.conf import settings
from django.template.loader import render_to_string
from django.apps import apps
from schema import Schema, And, Or, Use, Optional, SchemaError
import boto3
import xmltodict

from .utils import convert_answers


_title_case_re = re.compile(r'(?<!^)(?=[A-Z])')



class NoSessionError(Exception): pass


def make_aws_session(credentials):
    if credentials is None:
        return None

    session = boto3.Session(
        aws_access_key_id=credentials.get('AccessKeyId'),
        aws_secret_access_key=credentials.get('SecretAccessKey'),
        aws_session_token=credentials.get('SessionToken'),
        region_name=credentials.get('RegionName'),
        profile_name=credentials.get('ProfileName'),
    )
    return session

class MTurk:
    statuses = {
        'Submitted': None,
        'Approved': True,
        'Rejected': False,
    }

    environments = {
        "live": {
            "endpoint": "https://mturk-requester.us-east-1.amazonaws.com",
            "preview": "https://www.mturk.com/mturk/preview",
            "manage": "https://requester.mturk.com/mturk/manageHITs",
        },
        "sandbox": {
            "endpoint": "https://mturk-requester-sandbox.us-east-1.amazonaws.com",
            "preview": "https://workersandbox.mturk.com/mturk/preview",
            "manage": "https://requestersandbox.mturk.com/mturk/manageHITs",
        },
    }

    credentials_schema = Schema({
        'AccessKeyId': Use(str),
        'SecretAccessKey': Use(str),
        Optional('SessionToken'): Use(str),
        'Expiration': Or(datetime, Use(datetime.fromisoformat)),
        Optional('RegionName'): Use(str),
        Optional('ProfileName'): Use(str),
    })

    hit_type_schema = Schema({
        Optional('AutoApprovalDelayInSeconds'): Use(int),
        'AssignmentDurationInSeconds': Use(int),
        'Reward': Use(str),
        'Title': Use(str),
        Optional('Keywords'): Use(str),
        'Description': Use(str),
        Optional('QualificationRequirements'): [
            {
                'QualificationTypeId': Use(str),
                'Comparator': Or(
                    'LessThan', 'LessThanOrEqualTo', 'GreaterThan',
                    'GreaterThanOrEqualTo', 'EqualTo', 'NotEqualTo',
                    'Exists', 'DoesNotExist', 'In', 'NotIn',
                ),
                Optional('IntegerValues'): [
                    Use(int),
                ],
                Optional('LocaleValues'): [
                    {
                        'Country': Use(str),
                        'Subdivision': Use(str),
                    },
                ],
                # DEPRECATED: 'RequiredToPreview': Use(bool),
                Optional('ActionsGuarded'): Or(
                    'Accept', 'PreviewAndAccept', 'DiscoverPreviewAndAccept'
                ),
            },
        ],
        'LifetimeInSeconds': And(Use(int), lambda x: x > 0),
        Optional('MaxAssignments'): And(Use(int), lambda x: x > 0),
    })

    @classmethod
    def validate_credentials(cls, credentials):
        Schema(dict).validate(credentials)
        if 'Credentials' in credentials:
            credentials = credentials['Credentials']
        return cls.credentials_schema.validate(credentials)

    @classmethod
    def validate_hit_type(cls, hit_type):
        return cls.hit_type_schema.validate(hit_type)

    @classmethod
    def get_environment(cls):
        return cls.environments["sandbox" if settings.MTURK_SANDBOX else "live"]

    def __init__(self):
        self.environment = self.get_environment()
        self.client = None

    def connect(self, credentials):
        session = make_aws_session(credentials)
        if session:
            self.client = session.client(
                'mturk',
                region_name='us-east-1',
                endpoint_url=self.environment['endpoint'],
            )

    def create_hit_type(self, settings):
        response = self.client.create_hit_type(**settings)
        hit_type_id = response['HITTypeId']
        return hit_type_id

    def create_hit(self, task, project, hit_type_id, lifetime_in_seconds, max_assignments):
        task_id = task["task_id"]
        question = render_to_string('mturk_question.html', {
            "project": project,
            **task,
        })
        safe_question = question.replace(']]>', ']]]]><![CDATA[>')
        html_question = f'<HTMLQuestion xmlns="http://mechanicalturk.amazonaws.com/AWSMechanicalTurkDataSchemas/2011-11-11/HTMLQuestion.xsd"><HTMLContent><![CDATA[{safe_question}]]></HTMLContent><FrameHeight>0</FrameHeight></HTMLQuestion>'
        unique_request_token = f'{settings.UNIQUE_ID}-p{project.id}-t{task_id}'
        response = self.client.create_hit_with_hit_type(
            HITTypeId=hit_type_id,
            MaxAssignments=max_assignments,
            LifetimeInSeconds=lifetime_in_seconds,
            Question=html_question,
            RequesterAnnotation=str(task_id),
            UniqueRequestToken=unique_request_token,
            # UniqueRequestToken
        )
        hit_id = response['HIT']['HITId']
        Task = apps.get_model('video_eval_app', 'Task')
        Task.objects.filter(pk=task_id).update(turk_hit_id=hit_id)
        hit_group_id = response['HIT']['HITGroupId']
        return hit_group_id

    def get_assignments(self, hit_id, questions):
        response = self.client.list_assignments_for_hit(HITId=hit_id)
        assignments = {}
        for resp_assignment in response['Assignments']:
            assignment_id = resp_assignment['AssignmentId']
            is_approved = self.statuses[resp_assignment['AssignmentStatus']]
            worker_id = resp_assignment['WorkerId']
            raw_answers = xmltodict.parse(resp_assignment['Answer'])['QuestionFormAnswers']['Answer']
            if not isinstance(raw_answers, list):
                raw_answers = [raw_answers]
            answers = {
                answer['QuestionIdentifier']: answer['FreeText']
                for answer in raw_answers
            }
            assignments[assignment_id] = {
                'result': convert_answers(questions, turk_answers=answers),
                'is_approved': is_approved,
                'worker_id': worker_id,
            }
        return assignments


    def create_hits(self, project, tasks, messages):
        settings = copy.deepcopy(project.turk_settings)
        lifetime_in_seconds = settings.pop('LifetimeInSeconds')
        max_assignments = settings.pop('MaxAssignments')

        hit_type_id = self.create_hit_type(settings)

        try:
            hit_group_id = None
            for task in tasks:
                try:
                    hit_group_id = self.create_hit(task, project, hit_type_id, lifetime_in_seconds, max_assignments)
                except Exception as x:
                    messages.append(['error', str(x)])
        except Exception as x:
            messages.append(['error', str(x)])
        return hit_group_id

    def get_account_balance(self):
        balance = self.client.get_account_balance()['AvailableBalance']
        return balance
