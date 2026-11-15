def post_current_form(browser, url, data=None, **kwargs):
    """Submit the editor's optimistic revision just as its rendered form does."""
    from django.urls import resolve
    if resolve(url.split("?")[0]).url_name == "client_edit":
        response = browser.get(url)
        if response.status_code == 200 and response.context:
            data = {**(data or {}), "editor_revision": response.context["editor_revision"]}
    return browser.post(url, data, **kwargs)
