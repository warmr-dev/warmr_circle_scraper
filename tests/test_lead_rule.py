"""Commercial policy admission is independent of routing categories."""
import pytest
from circle_leads.classifier.ai_classifier import DESCRIPTION_VALUES, LEAD_RULE, SYSTEM_PROMPT, is_lead, read_description
from circle_leads.classifier.lead_classifier import ClassificationResult, meets_requirements


def test_rule_vocabulary_and_prompt_are_consistent():
    for key,values in LEAD_RULE.items():
        assert values <= set(DESCRIPTION_VALUES[key])
    for values in DESCRIPTION_VALUES.values():
        assert all(f'"{v}"' in SYSTEM_PROMPT for v in values)


@pytest.mark.parametrize('work_type',DESCRIPTION_VALUES['work_type'])
@pytest.mark.parametrize('signal',DESCRIPTION_VALUES['demand_signal'])
def test_any_category_is_demand_only_with_buyer_signal(work_type,signal):
    described={'author_role':'buyer','work_type':work_type,'demand_signal':signal}
    assert is_lead(described) == (signal != 'none')
    for author in ['seller','job_seeker','other']:
        assert not is_lead({**described,'author_role':author})


def test_incomplete_or_unknown_schema_is_no_verdict():
    assert read_description({'author_role':'buyer','wants':'service','work_type':'software'}) is None
    assert read_description({'author_role':'buyer','wants':'service','work_type':'software','demand_signal':'bad'}) is None


def test_routing_filters_never_drop_accepted_demand(dev_requirements):
    result=ClassificationResult(classification='LEAD',confidence=.9,
                                extracted={'job_title':'Videographer','skills':['Video']})
    assert meets_requirements(result,dev_requirements)
    assert not meets_requirements(result,dev_requirements.model_copy(update={'minimum_confidence':.95}))
