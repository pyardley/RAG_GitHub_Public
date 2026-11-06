# hwf-publicapp patterns

Taken from `hmcts/hwf-publicapp`. Page objects are SitePrism pages (`BasePage < SitePrism::Page`). Follow these shapes. Capybara is configured with `exact: true` and `match: :prefer_exact`, so a locator has to be the whole label, not a fragment of it.

## How one scenario runs

`features/address/address_married.feature`

```gherkin
Background: Navigating to the address page
  Given I am 'married' and on the address page with probate enabled

Scenario: Entering address with postcode
  When I enter my address with postcode
  Then I should be taken to contact page
```

| Step | Definition | What it calls |
| --- | --- | --- |
| `Given I am 'married' and on the address page with probate enabled` | `address_steps.rb` | `disable_address_lookup`, then `to_address_page('married')`, then `expect(address_page.content).to have_step_info` and `have_header` |
| `When I enter my address with postcode` | `address_steps.rb` | `address_page.submit_full_address` |
| `Then I should be taken to contact page` | `contact_steps.rb` | `expect(contact_page).to be_displayed` |

`to_address_page('married')` in `features/support/navigation_helper.rb` walks the earlier pages. Each call is a page method that answers that page and clicks Continue:

`to_fee_page` (checklist, then `checklist_continue`), `fee_page.submit_fee_yes`, `form_name_page.submit_valid_form_number`, `applying_on_behalf_page.submit_no`, `national_insurance_page.select_yes_and_enter_valid_ni`, `marital_status_page.submit_married`, `partner_national_insurance_page.submit_no_ni`, `savings_investment_page.low_amount_checked`, `benefit_page.submit_benefit_yes`, `probate_page.submit_probate_no`, `claim_page.submit_claim_no`, `dob_page.valid_partner_dob`, `personal_details_page.submit_full_names`.

The single branch of the same method swaps in `submit_single`, drops the partner National Insurance step, and calls `valid_dob` and `submit_full_name`. A new `to_*` method should extend the nearest existing one rather than re-click the journey from scratch.

`submit_full_address` fills three labels and clicks Continue:

```ruby
def submit_full_address
  address_page.street('102 Petty France')
  address_page.town('London')
  address_page.post_code('SW1H 9AJ')
  continue
end

def street(str)
  fill_in 'House number and street', with: str
end
```

Those label strings are `activemodel.attributes.forms/applicant_address` in `config/locales/en.yml`: `street: House number and street`, `town: Town or City`, `postcode: Postcode`. The view `app/views/questions/forms/_address.html.slim` prints them with `f.label`.

The error scenario `Then I should see 'Enter your postcode' error message` needs no new step. `common_steps.rb` checks the summary heading "There is a problem", the summary link, and `.govuk-error-message`. The sentence comes from `activemodel.errors.models.forms/applicant_address.attributes.postcode.blank`.

The timeout scenario calls `slow_submit_full_address`, which wraps the same fills in `travel 61.minutes`.

## SitePrism choice page

`features/support/page_objects/apply_type_page.rb` declares the URL, the content section, and one element per option. The method clicks the SitePrism element, then Continue. `navigation_helper.rb` calls `apply_type_page.applying_by_paper` and `apply_type_page.applying_by_online_service`.

```ruby
class ApplyTypePage < BasePage
  set_url '/applications/{app_id}/questions/apply_type'

  section :content, '#content' do
    element :step_info, '.govuk-caption-l', text: 'Step 20 of 22'
    element :header, 'h1', text: 'Will you be making your court or tribunal application on paper or using an online service?'
    element :paper, 'label', text: 'Paper'
    element :online, 'label', text: 'Online service'
  end

  def applying_by_paper
    apply_type_page.content.paper.click
    continue
  end

  def applying_by_online_service
    apply_type_page.content.online.click
    continue
  end
end
```

`content.paper` and `content.online` are the labels. The radio input stays hidden. `have_paper` is the assertion that the Paper label is there. The option text is `questions.apply_type.applying_method_paper` and `applying_method_online` in `en.yml` (`Paper`, `Online service`).

A new radio or checkbox page should look like this. Do not add another `find('label', text: '...')`. `common_steps.rb` has one of those for "Online service"; new steps should call `applying_by_online_service` instead.

## Page object skeleton

Text fields on `AddressPage` use `fill_in`. The heading and caption are still SitePrism elements, same as `ApplyTypePage`.

```ruby
class AddressPage < BasePage
  include ActiveSupport::Testing::TimeHelpers

  set_url '/applications/{app_id}/questions/applicant_address'

  section :content, '#content' do
    element :step_info, '.govuk-caption-l', text: 'Step 21 of 25'
    element :header, 'h1', text: 'What is your address?'
  end
end
```

`have_step_info` and `have_header` are the SitePrism predicates for those elements. They fail when the text differs, which is what you want for a heading assertion. The caption is hard-coded and is not the same on every page: address says `Step 21 of 25`, apply type says `Step 20 of 22`. Copy the caption from the live page.

`features/support/generic_helpers.rb` exposes the instance:

```ruby
def address_page
  @address_page ||= AddressPage.new
end

def continue
  base_page.content.continue_button.click
end
```

`BasePage` defines `continue_button` as `input[type="submit"]` inside `#content`. Reuse `continue`. Do not click a second selector for the same button. The checklist uses `checklist_continue` because that button is not the submit input.

## Locators this suite actually uses

| Control | How this suite finds it | Example |
| --- | --- | --- |
| Text field with a label | `fill_in '<label>', with: value` | `fill_in 'Postcode', with: 'SW1H 9AJ'` |
| Yes / No and other radios | SitePrism `element :yes, 'label', text: 'Yes'` then `content.yes.click` | `ApplyTypePage` clicks `content.paper` and `content.online`. `FeePage#submit_fee_no` clicks `content.no` |
| Savings bands | `element :low_amount, 'label', text: 'Less than £4,250'` then click | The visible text is the label, not the radio input |
| Field with no useful label | `element :contact_email, '#contact_email'` then `.set` | `ContactPage#valid_email` |
| Heading or caption | `element :header, 'h1', text: '...'` inside the content section | Assert with `have_header` |
| Next page | `set_url` plus `expect(page).to be_displayed` | Contact is `/applications/{app_id}/questions/contact` |
| Link by its text | `click_on 'Enter address manually'` or `element :fees_link, 'a', text: '...'` | Demo address entry |

`fill_in`, `choose`, `check`, `uncheck`, and `select` match id, name, placeholder, aria-label, and the label text. `click_button` matches id, name, value, title, or text. `element` and `find` take CSS, plus an optional `text:` filter. A CSS or XPath copied from the browser belongs in `element` or `find`. A label or button caption belongs in `fill_in`, `choose`, or `click_button`. On these GOV.UK pages the label text is the stable locator. Prefer it to a generated id.

Click the label for a radio or checkbox. The input is visually hidden. The label text is what the tester sees.

## Step definition shape

```ruby
Given(/^I am '([^"]*)' and on the address page with probate enabled$/) do |status|
  disable_address_lookup
  status == 'married' ? to_address_page('married') : to_address_page('single')
  expect(address_page.content).to have_step_info
  expect(address_page.content).to have_header
end

When(/^I enter my address with postcode$/) do
  address_page.submit_full_address
end
```

Keep captured text in the feature (`'married'`, the error sentence). Keep data that the feature does not mention, such as `102 Petty France`, inside the page method, next to the other fixtures.

Search the step files before adding a phrase. Shared phrases already live in `common_steps.rb` (`I click continue`, `I should see '<text>' error message`, `I should see '<text>' header`) and `homepage_steps.rb` (`I should see the home page`).

Question URLs are `/applications/{app_id}/questions/<form_id>`. The h1 and the step caption come from `config/locales/en.yml` under `questions.<form_id>`. `page_number` is the index of that symbol in `Settings.navigation` plus one. Address lookup is stubbed with `disable_address_lookup`. On a host that really shows lookup, click **Enter address manually** before filling the fields (`AddressPage#submit_full_address_demo`).

Run one scenario from the app checkout:

```bash
bundle exec cucumber features/address/address_married.feature:6
```

To drive the given environment URL, set `CAPYBARA_APP_HOST` to that URL. A host other than localhost selects Firefox. Confirm the browser host has no `:3000` suffix, because this suite sets `Capybara.always_include_port = true`.
