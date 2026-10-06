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
    described={'hiring_scope':'not_hiring','need_owner':'current_author','service_direction':'seeking_help','post_purpose':'demand','author_role':'buyer','work_type':work_type,'demand_signal':signal}
    assert is_lead(described) == (signal != 'none')
    for author in ['seller','job_seeker']:
        assert not is_lead({**described,'author_role':author})
    assert is_lead({**described,'author_role':'other'}) == (signal != 'none')


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
    assert not is_lead({'hiring_scope':'not_hiring','need_owner':'current_author','service_direction':'seeking_help','author_role':'buyer','post_purpose':purpose,
                        'demand_signal':'solution_exploration','work_type':'marketing'})


def test_mixed_post_with_actual_demand_is_not_excluded_by_informational_content():
    assert is_lead({'hiring_scope':'not_hiring','need_owner':'current_author','service_direction':'seeking_help','author_role':'buyer','post_purpose':'mixed_demand',
                    'demand_signal':'explicit_demand','work_type':'other'})


@pytest.mark.parametrize('owner', ['other_people','none'])
def test_other_peoples_demand_cannot_become_current_author_candidate(owner):
    assert not is_lead({'hiring_scope':'not_hiring','need_owner':owner,'author_role':'buyer',
                        'service_direction':'seeking_help','post_purpose':'mixed_demand',
                        'demand_signal':'problem_intent','work_type':'marketing'})


def test_helper_can_represent_a_buyer_without_inheriting_supplier_identity():
    assert is_lead({'hiring_scope':'not_hiring','need_owner':'represented_buyer','author_role':'other',
                    'service_direction':'seeking_help','post_purpose':'demand',
                    'demand_signal':'explicit_demand','work_type':'content'})


def test_generic_hiring_scope_cannot_pass_as_commercial_demand():
    assert not is_lead({'hiring_scope':'generic_inhouse','need_owner':'current_author',
                        'author_role':'buyer','service_direction':'seeking_help',
                        'post_purpose':'demand','demand_signal':'hiring_signal',
                        'work_type':'admin'})


@pytest.mark.parametrize('scope',['specialist_or_leadership','contract_or_external_service'])
def test_specialist_and_contract_hiring_do_not_require_software_skills(scope):
    assert is_lead({'hiring_scope':scope,'need_owner':'current_author',
                    'author_role':'buyer','service_direction':'seeking_help',
                    'post_purpose':'demand','demand_signal':'hiring_signal',
                    'work_type':'other'})


def test_generic_vacancy_does_not_suppress_separate_commercial_need_in_mixed_post():
    assert is_lead({'hiring_scope':'generic_inhouse','need_owner':'current_author',
                    'author_role':'buyer','service_direction':'seeking_help',
                    'post_purpose':'mixed_demand','demand_signal':'explicit_demand',
                    'work_type':'marketing'})
