# et-full-system patterns

Taken from `hmcts/et-full-system`. The stack is the same Cucumber, Capybara, and SitePrism shape. This checkout is the umbrella suite for the employment tribunal: ET1 (the claimant), ET3 (the respondent), diversity, and admin. Classes live under `EtFullSystem::Test`. Follow this file when the checkout is et-full-system.

## Where the files live

| Piece | Path |
| --- | --- |
| Features | `features/et1/`, `features/et3/`, `features/diversity/`, plus `ccd/`, `email_notifications/`, `end_to_end/`, `pdf/` |
| Step definitions | `features/step_definitions/`, named `et1_*_steps.rb`, `et3_*_steps.rb`, `diversity_*_steps.rb` |
| Page objects | `features/support/page_objects/et1/`, `et3/`, `diversity/`, `admin/` |
| ET1 and ET3 page accessors | `features/support/helpers/pages.rb` |
| Diversity page accessors | `features/support/helpers/diversity_pages.rb` |
| ET1 journey | `features/support/helpers/et1/et1_claim_helper.rb` |
| ET3 journey | `features/support/helpers/et3/et3_response_helper.rb` |
| Diversity journey | `features/support/helpers/diversity_helper.rb` |
| Test data | `features/factories/` (FactoryBot) |

`features/support/generic_helpers.rb` only builds the messaging object. Page accessors are not in that file.

`features/support/env.rb` loads dotenv, Capybara, and FactoryBot. It is not a generated cucumber-rails file. `features/support/test_common.rb` mixes the page and journey modules into Cucumber with `World`. `features/support/page_objects.rb` requires `base_page.rb` and then globs `page_objects/**/*.rb`. `features/support/helpers.rb` globs `helpers/**/*.rb`. A new page file and a new helper file are loaded by those globs.

Accessors construct a new page on every call:

```ruby
def et1_claim_type_page
  EtFullSystem::Test::Et1::ClaimTypePage.new
end
```

Add a new `def et1_<name>_page` in `pages.rb` when the ET1 or ET3 class is new. A diversity page is a class method on `DiversityPages`, called as `diversity_pages.claim_type_page`.

## How a scenario starts

An ET1 scenario builds FactoryBot records, then walks the earlier pages through the journey helper.

`features/et1/et1_claim_type.feature`:

```gherkin
@et1 @javascript
Feature:
  As About the claim page
  I want to ensure that user can submit their About the claim in Welsh or in English

  Background: About the claim page
    Given a claimant is on About the claim page

  Scenario: Select all claim types
    When I select all claim types
    Then I should be on Claim details page
```

`et1_claim_type_page_steps.rb` fills the Given by creating `:claimant`, `:representative`, `:respondent`, `:employment`, and `:claim`, then calling `start_a_new_et1_claim`, `et1_answer_login`, `et1_answer_claimant_questions`, and the other `et1_answer_*` methods up to the page under test. Those methods live in `Et1ClaimHelper`. Extend the nearest `et1_answer_*` method. A new Given should call that chain rather than re-click the claim from the apply page.

`start_a_new_et1_claim` loads `et1_apply_page` and calls `start_a_claim`. ET3 starts with `start_a_new_et3_response` (`start_page.next`). Diversity starts with `diversity_load_page`.

English and Welsh are both first-class. `load_et1_start_page(in_language: :cy)` calls `switch_to_welsh`. `TEST_LOCALE=cy` selects Welsh for the run. The default is English.

## The advance button

ET1 form pages click **Save and continue** through their own `save_and_continue`:

```ruby
gds_submit_button :save_and_continue_button, t('helpers.submit.update')

def save_and_continue
  save_and_continue_button.click
end
```

`Et1ClaimHelper` calls `set`, then `save_and_continue`, on each page. The apply page is the exception: `gds_submit_button :start_a_claim_button` and `start_a_claim` clicks it.

ET3 pages advance with `next`. `Et3::StartPage#next` clicks `start_button`. Later ET3 pages, such as the respondents details page, define their own `next`, and `Et3ResponseHelper` calls `respondents_details_page.next`.

Diversity pages define `save_and_continue`. The journey helper calls `set_for` on each page, then `diversity_submit_form`.

## Locators are locale keys

Elements do not store the English sentence. They store an i18n key, and the `et_test_helpers` gem (tag `v1.3.2`) turns that key into the text for the current locale. `ClaimTypePage` is the form-page pattern:

```ruby
module EtFullSystem
  module Test
    module Et1
      class ClaimTypePage < BasePage
        element :page_header, :page_title, 'claims.claim_type.header', exact: false
        gds_checkboxes :unfair_dismissal, :'claims.claim_type.unfair_dismissal'
        gds_radios :send_to_relevant_person, :'simple_form.labels.claim_type.send_claim_to_whistleblowing_entity'
        gds_text_area :claim_type_other_claim_details, :'simple_form.labels.claim_type.other_claim_details'
        gds_error_summary :error_message, :'shared.error_notification.default_message'

        def save_and_continue
          save_and_continue_button.click
        end

        def set(claim)
          return if claim.nil? || claim.claim_types.nil?

          unfair_dismissal.set(claim.claim_types.unfair_dismissal)
          claim_type_other_claim_details.set(claim.claim_types.to_h[:other_type_of_claim_details])
        end
      end
    end
  end
end
```

`Et1::BasePage` subclasses `EtFullSystem::Test::BasePage`, which subclasses `SitePrism::Page`. Put a new ET1 page in that subclass chain. Use `gds_radios`, `gds_checkboxes`, `gds_text_input`, `gds_text_area`, `gds_submit_button`, and `gds_error_summary` for GOV.UK controls. Each component answers `.set`.

`has_correct_translation?` expects every heading, hint, and button on that page. A copy-text scenario calls it and expects `true`. Add the new element to that method when the scenario checks the copy.

`has_missing_claim_type_error_message?` expects the `gds_error_summary`. The summary text is `activemodel.errors.models.claim_type.attributes.blank`.

## What the step calls

```ruby
Then('I select all claim types') do
  claim = FactoryBot.create(:claim)
  et1_claim_type_page.set(claim)
end

Then('I should be on Claim details page') do
  et1_claim_type_page.save_and_continue
  expect(et1_claim_details_page).to have_page_header
end
```

The feature names the action. The factory supplies the answers. `set` writes them. `have_page_header` is the arrival check on a form page, because most form pages have no `set_url`.

Start pages do set a URL, from configuration: `ApplyPage` uses `Configuration.et1_url`, `Et3::StartPage` uses `Configuration.et3_url`, and the diversity landing page uses `Configuration.diversity_url`. A page with a stable path can use `set_url`, as `Et1::CaseHeardByPage` does with `/en/apply/case-heard-by`. `expect(et1_your_feedback_page).to be_displayed` is for a page that has `set_url`.

Values the scenario does not quote belong in the factory (`features/factories/`) or in the `set` method. A scenario that must choose one option can call the component directly: `et1_claim_type_page.whistleblowing_claim.is_whistleblowing.set(:yes)`.

## Run it

The suite drives the dockerised stack (ET1, ET3, admin, and the API behind one nginx). Start that stack first with `./bin/dev/test_server up`, and wait until Passenger reports multi-application mode. The default host is `http://et.127.0.0.1.nip.io:3100`.

From the et-full-system checkout:

```bash
bundle exec cucumber features/et1/et1_claim_type.feature
```

`cucumber.yml` already excludes `@broken`. `DRIVER=chrome` opens Chrome. `TEST_LOCALE=cy` runs the Welsh copy. `ENVIRONMENT=dev` or `ENVIRONMENT=staging` points the same suite at those hosts.
