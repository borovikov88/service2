from django import template


register = template.Library()


@register.simple_tag
def crm_phone_input(bound_field):
    """Render CRM phones without the account-only Russian browser mask.

    Keep Django's bound value, prefix, errors and escaping. Do not reimplement
    phone parsing in JavaScript: phone_utils handles normalization on save.
    Overriding render attributes does not mutate shared form/account widgets.
    """
    attrs = dict(bound_field.field.widget.attrs)
    attrs["class"] = " ".join(
        name for name in attrs.get("class", "").split() if name != "phone-mask"
    )
    attrs.update({"inputmode": "tel", "autocomplete": "tel", "data-crm-phone": "true"})
    return bound_field.as_widget(attrs=attrs)
