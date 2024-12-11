from icecream import ic

import re
import json
import copy
from contextlib import asynccontextmanager


from django.conf import settings
from django.template.loader import render_to_string
from django.apps import apps
import aioboto3
import xmltodict

from .utils import convert_answers


_title_case_re = re.compile(r'(?<!^)(?=[A-Z])')





def make_aws_session(credentials):
    session = aioboto3.Session(
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

    @classmethod
    def get_environment(cls):
        return cls.environments["sandbox" if settings.MTURK_SANDBOX else "live"]

    def __init__(self, credentials):
        self.credentials = credentials
        self.environment = self.get_environment()

    @asynccontextmanager
    async def connect(self):
        session = make_aws_session(self.credentials)
        async with session.client(
            'mturk',
            region_name='us-east-1',
            endpoint_url=self.environment['endpoint'],
        ) as client:
            yield client

    async def _create_hit_type(self, client, settings):
        response = await client.create_hit_type(**settings)
        hit_type_id = response['HITTypeId']
        return hit_type_id

    async def _create_hit(self, client, task, project, hit_type_id, lifetime_in_seconds, max_assignments):
        task_id = task["task_id"]
        question = render_to_string('mturk_question.html', {
            "project": project,
            **task,
        })
        safe_question = question.replace(']]>', ']]]]><![CDATA[>')
        html_question = f'<HTMLQuestion xmlns="http://mechanicalturk.amazonaws.com/AWSMechanicalTurkDataSchemas/2011-11-11/HTMLQuestion.xsd"><HTMLContent><![CDATA[{safe_question}]]></HTMLContent><FrameHeight>0</FrameHeight></HTMLQuestion>'
        unique_request_token = f'{settings.UNIQUE_ID}-p{project.id}-t{task_id}'
        response = await client.create_hit_with_hit_type(
            HITTypeId=hit_type_id,
            MaxAssignments=max_assignments,
            LifetimeInSeconds=lifetime_in_seconds,
            Question=html_question,
            RequesterAnnotation=str(task_id),
            UniqueRequestToken=unique_request_token,
        )
        hit_id = response['HIT']['HITId']
        # TODO: ameliorate
        Task = apps.get_model('video_eval_app', 'Task')
        await Task.objects.filter(pk=task_id).aupdate(turk_hit_id=hit_id)
        hit_group_id = response['HIT']['HITGroupId']
        return hit_group_id

    async def create_hits(self, client, project, tasks, messages):
        settings = copy.deepcopy(project.turk_settings)
        lifetime_in_seconds = settings.pop('LifetimeInSeconds')
        max_assignments = settings.pop('MaxAssignments')

        hit_type_id = await self._create_hit_type(client, settings)

        hit_group_id = None
        for task in tasks:
            try:
                hit_group_id = await self._create_hit(client, task, project, hit_type_id, lifetime_in_seconds, max_assignments)
            except Exception as x:
                messages.append(['error', str(x)])
        return hit_group_id

    async def get_assignments(self, client, hit_id, questions):
        response = await client.list_assignments_for_hit(HITId=hit_id)
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

    async def get_account_balance(self):
        async with self.connect() as client:
            balance = (await client.get_account_balance())['AvailableBalance']
        return balance
