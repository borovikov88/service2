from django import forms

from pool_service.models import DevelopmentIteration, DevelopmentTask


class BootstrapModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            css_class = "form-select" if isinstance(widget, forms.Select) else "form-control"
            widget.attrs["class"] = f"{widget.attrs.get('class', '')} {css_class}".strip()
            if isinstance(widget, forms.Textarea):
                widget.attrs.setdefault("rows", 4)


class DevelopmentTaskCreateForm(BootstrapModelForm):
    class Meta:
        model = DevelopmentTask
        fields = ["title", "description", "business_goal", "priority", "definition_of_done"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 7}),
            "business_goal": forms.Textarea(attrs={"rows": 4}),
            "definition_of_done": forms.Textarea(attrs={"rows": 6}),
        }


class DevelopmentTaskUpdateForm(BootstrapModelForm):
    class Meta:
        model = DevelopmentTask
        fields = [
            "priority",
            "status",
            "current_stage",
            "completed_work",
            "current_activity",
            "blockers",
            "final_summary",
            "execution_result",
        ]
        widgets = {
            "completed_work": forms.Textarea(attrs={"rows": 4}),
            "current_activity": forms.Textarea(attrs={"rows": 4}),
            "blockers": forms.Textarea(attrs={"rows": 4}),
            "final_summary": forms.Textarea(attrs={"rows": 4}),
            "execution_result": forms.Textarea(attrs={"rows": 4}),
        }


class DevelopmentIterationForm(BootstrapModelForm):
    class Meta:
        model = DevelopmentIteration
        fields = [
            "executor_type",
            "status",
            "prompt",
            "response",
            "result_summary",
            "started_at",
            "completed_at",
            "changed_files",
            "test_result",
            "tests_passed",
            "tests_failed",
            "technical_errors",
            "reviewer_notes",
            "next_prompt",
        ]
        widgets = {
            "started_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "completed_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "prompt": forms.Textarea(attrs={"rows": 8}),
            "response": forms.Textarea(attrs={"rows": 8}),
            "result_summary": forms.Textarea(attrs={"rows": 4}),
            "changed_files": forms.Textarea(attrs={"rows": 4}),
            "test_result": forms.Textarea(attrs={"rows": 4}),
            "technical_errors": forms.Textarea(attrs={"rows": 4}),
            "reviewer_notes": forms.Textarea(attrs={"rows": 4}),
            "next_prompt": forms.Textarea(attrs={"rows": 6}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["started_at"].input_formats = ["%Y-%m-%dT%H:%M"]
        self.fields["completed_at"].input_formats = ["%Y-%m-%dT%H:%M"]

    def clean(self):
        cleaned_data = super().clean()
        started_at = cleaned_data.get("started_at")
        completed_at = cleaned_data.get("completed_at")
        if started_at and completed_at and completed_at < started_at:
            self.add_error("completed_at", "Дата завершения не может быть раньше даты начала.")
        return cleaned_data
