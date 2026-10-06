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
    described={'service_direction':'seeking_help','post_purpose':'demand','author_role':'buyer','work_type':work_type,'demand_signal':signal}
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


@pytest.mark.parametrize('purpose', ['information','supplier_offer','social','career_advice','generic_inhouse_hiring','closed_need'])
def test_post_purpose_excludes_non_demand_even_with_commercial_vocabulary(purpose):
    # A model can overinterpret commercial vocabulary as a demand signal.
    # Its semantic description of an informational/closed/supplier purpose
    # still prevents admission without a current-author request/problem.
    assert not is_lead({'service_direction':'seeking_help','author_role':'buyer','post_purpose':purpose,
                        'demand_signal':'solution_exploration','work_type':'marketing'})


def test_mixed_post_with_actual_demand_is_not_excluded_by_informational_content():
    assert is_lead({'service_direction':'seeking_help','author_role':'buyer','post_purpose':'mixed_demand',
                    'demand_signal':'explicit_demand','work_type':'other'})
