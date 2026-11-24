from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from .models import Bounty, Category, Claim, Report, ReportReason


class BountyForm(forms.ModelForm):
    deadline = forms.DateTimeField(
        widget=forms.DateTimeInput(
            attrs={
                "type": "datetime-local",
                "class": "input-text",
            },
            format="%Y-%m-%dT%H:%M",
        ),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"],
        help_text="Deadline must be in the future.",
    )

    class Meta:
        model = Bounty
        fields = ["title", "description", "category", "reward", "deadline"]
        widgets = {
            "title": forms.TextInput(attrs={
                "class": "input-text",
                "placeholder": "e.g. Debug ESP32 Bluetooth communication failure",
                "maxlength": "80",
            }),
            "description": forms.Textarea(attrs={
                "class": "input-textarea",
                "placeholder": "Describe what needs to be solved, inputs provided, and acceptance criteria...",
                "rows": 5,
            }),
            "category": forms.Select(attrs={"class": "input-select"}),
            "reward": forms.NumberInput(attrs={
                "class": "input-text",
                "min": "10",
                "max": "10000",
                "step": "10",
                "placeholder": "₹ Amount (10 to 10,000)",
            }),
        }

    def clean_title(self):
        title = self.cleaned_data.get("title", "").strip()
        if len(title) < 8:
            raise ValidationError("Title must be at least 8 characters long.")
        if len(title) > 80:
            raise ValidationError("Title cannot exceed 80 characters.")
        return title

    def clean_description(self):
        desc = self.cleaned_data.get("description", "").strip()
        if len(desc) < 20:
            raise ValidationError("Description must be at least 20 characters long.")
        if len(desc) > 1000:
            raise ValidationError("Description cannot exceed 1000 characters.")
        return desc

    def clean_reward(self):
        reward = self.cleaned_data.get("reward")
        if reward is None or reward < 10 or reward > 10000:
            raise ValidationError("Reward must be between ₹10 and ₹10,000.")
        return reward

    def clean_deadline(self):
        deadline = self.cleaned_data.get("deadline")
        if deadline and deadline <= timezone.now():
            raise ValidationError("Deadline must be in the future.")
        return deadline


class ClaimForm(forms.ModelForm):
    class Meta:
        model = Claim
        fields = ["message"]
        widgets = {
            "message": forms.Textarea(attrs={
                "class": "input-textarea",
                "placeholder": "Why are you qualified to solve this? Include relevant skills, past lab work, or tools you have...",
                "rows": 4,
            }),
        }

    def clean_message(self):
        msg = self.cleaned_data.get("message", "").strip()
        if len(msg) < 10:
            raise ValidationError("Message must be at least 10 characters.")
        if len(msg) > 500:
            raise ValidationError("Message cannot exceed 500 characters.")
        return msg


class ReportForm(forms.ModelForm):
    class Meta:
        model = Report
        fields = ["reason", "note"]
        widgets = {
            "reason": forms.Select(attrs={"class": "input-select"}),
            "note": forms.Textarea(attrs={
                "class": "input-textarea",
                "placeholder": "Add specific context for campus moderators...",
                "rows": 3,
            }),
        }
