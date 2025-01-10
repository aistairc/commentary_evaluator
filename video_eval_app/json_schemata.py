from datetime import datetime
import json

from schema import Schema, And, Or, Use, Optional, SchemaError


credentials_schema = Schema({
    'AccessKeyId': Use(str),
    'SecretAccessKey': Use(str),
    Optional('SessionToken'): Use(str),
    'Expiration': Or(datetime, Use(datetime.fromisoformat)),
    Optional('RegionName'): Use(str),
    Optional('ProfileName'): Use(str),
    Optional('Location'): Use(str),
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

def validate_question_has_options(question):
    has_options = 'options' in question and isinstance(question['options'], list)
    need_options = question['options'] in {'checkbox', 'radio'}
    if bool(has_options) != bool(need_options):
        raise SchemaError("'options' should only be present when 'type' is 'checkbox' or 'radip'")

questions_schema = Schema([
    {
        "id": Use(str),
        "instruction": Use(str),
        Optional("type", default="text"): Or(
            "text", "textarea", "radio", "checkbox",
        ),
        Optional("options"): [
            {
                "value": Or(Use(int), Use(str)),
                "text": Use(str),
            }
        ],
    },
])

def parse_credentials(credentials_text):
    credentials = json.loads(credentials_text)
    Schema(dict).validate(credentials)
    if 'Credentials' in credentials:
        credentials = credentials['Credentials']
    return credentials_schema.validate(credentials)

def parse_hit_type(hit_type_text):
    hit_type = json.loads(hit_type_text)
    return hit_type_schema.validate(hit_type)

def parse_questions(questions_text):
    questions = json.loads(questions_text)
    return questions_schema.validate(questions)
