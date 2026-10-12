from home_budget_pipeline.web.product_categories import category_for_product


def test_accepted_salsa_resolves_without_defaulting_unknown_products_to_groceries():
    item = {'item_name': 'Cep Pic Med', 'budget_category': 'Uncategorized'}
    assert category_for_product(item, 'Old El Paso Salsa Picante Medium', [], {'Groceries'}).category == 'Groceries'
    assert category_for_product(item, 'Mysterious object', [], {'Groceries'}) is None
    assert category_for_product(item, 'Salsa', [], set()) is None


def test_existing_and_manual_categories_are_preserved():
    for fields in [{'budget_category': 'Indoor Supplies'}, {'category_source': 'manual'},
                   {'category_rationale': 'human-approved category override'}]:
        assert category_for_product({'item_name': 'Cep Pic Med', **fields}, 'Salsa', [], {'Groceries'}) is None


def test_approved_rules_respect_scope_and_match_original_or_accepted_description():
    item = {'item_name': 'Cep Pic Med', 'source': 'scanned', 'store_name': 'Sobeys'}
    rule = {'source': 'scanned', 'merchant': 'Sobeys', 'match_type': 'exact',
            'match_text': 'Cep Pic Med', 'category_name': 'Indoor Supplies'}
    assert category_for_product(item, 'Salsa', [rule], {'Groceries', 'Indoor Supplies'}).category == 'Indoor Supplies'
    rule['merchant'] = 'Costco'
    assert category_for_product(item, 'Salsa', [rule], {'Groceries', 'Indoor Supplies'}).category == 'Groceries'
