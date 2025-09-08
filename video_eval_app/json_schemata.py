from datetime import datetime
import json

from schema import Schema, And, Or, Use, Optional, SchemaError


# Custom validation exceptions
class CredentialValidationError(ValueError):
    """Raised when credential validation fails (not JSON parsing)"""
    pass

class JSONParseError(ValueError):
    """Raised when JSON parsing fails"""
    pass


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

cuts_schema = Schema([
    Or(
        [Or(Use(int), Use(float))],  # Single element: [start]
        [Or(Use(int), Use(float)), Or(Use(int), Use(float))]  # Two elements: [start, end]
    )
])

def parse_cuts(cuts_text):
    cuts = json.loads(cuts_text)
    return cuts_schema.validate(cuts)

def parse_credentials(credentials_text):
    try:
        credentials = json.loads(credentials_text)
    except json.JSONDecodeError:
        raise JSONParseError("AWS credentials are not valid JSON")
    
    try:
        Schema(dict).validate(credentials)
        if 'Credentials' in credentials:
            credentials = credentials['Credentials']
        return credentials_schema.validate(credentials)
    except SchemaError as x:
        raise CredentialValidationError(f"AWS credentials validation failed: {x}")

def parse_hit_type(hit_type_text):
    hit_type = json.loads(hit_type_text)
    return hit_type_schema.validate(hit_type)

def parse_questions(questions_text):
    questions = json.loads(questions_text)
    return questions_schema.validate(questions)
