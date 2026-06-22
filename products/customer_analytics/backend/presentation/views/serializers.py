import json

from django.db import IntegrityError, transaction

from drf_spectacular.utils import extend_schema_field
from pydantic import ValidationError as PydanticValidationError
from rest_framework import serializers

from posthog.api.shared import UserBasicSerializer
from posthog.api.tagged_item import TaggedItemSerializerMixin
from posthog.exceptions import Conflict
from posthog.models import OrganizationMembership

from products.customer_analytics.backend.models import (
    DATA_TYPE_BY_DISPLAY_TYPE,
    Account,
    CustomerJourney,
    CustomerProfileConfig,
    CustomPropertyDefinition,
    DataType,
    DisplayType,
)
from products.notebooks.backend.models import Notebook

_ACCOUNT_ASSIGNMENT_SCHEMA = {
    "type": "object",
    "nullable": True,
    "properties": {
        "id": {"type": "integer"},
        "email": {"type": "string"},
    },
    "required": ["id", "email"],
}

_ACCOUNT_PROPERTIES_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "csm": _ACCOUNT_ASSIGNMENT_SCHEMA,
        "account_executive": _ACCOUNT_ASSIGNMENT_SCHEMA,
        "account_owner": _ACCOUNT_ASSIGNMENT_SCHEMA,
        "stripe_customer_id": {"type": "string", "nullable": True},
        "hubspot_deal_id": {"type": "string", "nullable": True},
        "billing_id": {"type": "string", "nullable": True},
        "sfdc_id": {"type": "string", "nullable": True},
        "zendesk_id": {"type": "string", "nullable": True},
        "slack_channel_id": {"type": "string", "nullable": True},
        "usage_dashboard_link": {"type": "string", "nullable": True},
    },
}


@extend_schema_field(_ACCOUNT_PROPERTIES_SCHEMA)
class AccountPropertiesField(serializers.JSONField):
    pass


class CustomerProfileConfigSerializer(serializers.ModelSerializer):
    content = serializers.JSONField(required=False, allow_null=True, default=dict)
    sidebar = serializers.JSONField(required=False, allow_null=True, default=dict)

    class Meta:
        model = CustomerProfileConfig
        fields = [
            "id",
            "scope",
            "content",
            "sidebar",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "created_at",
            "updated_at",
        ]

    @staticmethod
    def validate_scope(value):
        if value not in dict(CustomerProfileConfig.Scope.choices):
            raise serializers.ValidationError(
                f"Invalid scope '{value}'. Must be one of: {', '.join(dict(CustomerProfileConfig.Scope.choices).keys())}"
            )
        return value

    def validate_content(self, value):
        return self._validate_json(field="content", value=value)

    def validate_sidebar(self, value):
        return self._validate_json(field="sidebar", value=value)

    def _validate_json(self, field: str, value):
        self.fields[field].run_validation(value)

        if value is None:
            return {}

        if not isinstance(value, dict | list):
            raise serializers.ValidationError(f"Invalid value for field '{field}'")

        try:
            json.dumps(value)
        except (ValueError, TypeError):
            raise serializers.ValidationError(f"Invalid value for field '{field}'")

        return value

    def create(self, validated_data):
        request = self.context["request"]
        validated_data["created_by"] = request.user
        validated_data["team_id"] = self.context["team_id"]
        return super().create(validated_data)


class CustomerJourneySerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomerJourney
        fields = ["id", "insight", "name", "description", "created_at", "created_by", "updated_at"]
        read_only_fields = ["id", "created_at", "created_by", "updated_at"]

    def validate_insight(self, value):
        if value.team_id != self.context["team_id"]:
            raise serializers.ValidationError("The insight does not belong to this team.")
        return value

    def create(self, validated_data):
        from django.db import IntegrityError

        from posthog.exceptions import Conflict

        validated_data["created_by"] = self.context["request"].user
        validated_data["team_id"] = self.context["team_id"]
        try:
            return super().create(validated_data)
        except IntegrityError:
            raise Conflict("A customer journey already exists for this insight.")


class AccountSerializer(TaggedItemSerializerMixin, serializers.ModelSerializer):
    """A Customer Analytics account — a logical grouping used to assign customer-success ownership."""

    name = serializers.CharField(
        max_length=400,
        help_text="Human-readable name of the account.",
    )
    external_id = serializers.CharField(
        max_length=400,
        required=False,
        allow_null=True,
        allow_blank=True,
        help_text=(
            "Identifier linking this account to its source customer — the analytics group key "
            "(the customer's organization id), used to match billing and external records. Optional."
        ),
    )
    properties = AccountPropertiesField(
        source="_properties",
        required=False,
        allow_null=True,
        help_text=(
            "Typed account properties: assignment fields (csm, account_executive, account_owner) "
            "and external system identifiers (stripe_customer_id, hubspot_deal_id, billing_id, "
            "sfdc_id, zendesk_id, slack_channel_id, usage_dashboard_link). Defaults to an empty "
            "object. Unknown keys are rejected."
        ),
    )
    tags = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="Tag names attached to the account. Pass a list to replace existing tags.",
    )
    notebooks = serializers.SerializerMethodField(
        help_text=(
            "Short IDs of the internal notebooks linked to this account, used to persist investigations, "
            "call notes, and other free-form context. Empty list if no notebooks have been created for the account."
        )
    )

    class Meta:
        model = Account
        fields = [
            "id",
            "name",
            "external_id",
            "properties",
            "tags",
            "notebooks",
            "created_at",
            "created_by",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "notebooks",
            "created_at",
            "created_by",
            "updated_at",
        ]

    @extend_schema_field({"type": "array", "items": {"type": "string"}})
    def get_notebooks(self, obj: Account) -> list[str]:
        return [link.notebook.short_id for link in obj.notebooks.all()]

    def validate_properties(self, value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError("properties must be a JSON object.")
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            raise serializers.ValidationError("properties must be JSON-serializable.")
        return value

    def create(self, validated_data):
        properties = validated_data.pop("_properties", {})
        validated_data.pop("tags", None)
        try:
            with transaction.atomic():
                account = Account.objects.create_account(
                    team=self.context["get_team"](),
                    created_by=self.context["request"].user,
                    name=validated_data["name"],
                    external_id=validated_data.get("external_id"),
                    properties=properties,
                )
                self._attempt_set_tags(self.initial_data.get("tags"), account)
        except PydanticValidationError as exc:
            raise serializers.ValidationError({"properties": _format_pydantic_errors(exc)})
        except IntegrityError:
            raise Conflict("An account with this external_id already exists for this team.")
        return account

    def update(self, instance, validated_data):
        update_kwargs: dict = {}
        if "name" in validated_data:
            update_kwargs["name"] = validated_data["name"]
        if "external_id" in validated_data:
            update_kwargs["external_id"] = validated_data["external_id"]
        if "_properties" in validated_data:
            update_kwargs["properties"] = validated_data["_properties"]

        try:
            with transaction.atomic():
                account = Account.objects.update_account(instance, **update_kwargs)
                self._attempt_set_tags(self.initial_data.get("tags"), account)
        except PydanticValidationError as exc:
            raise serializers.ValidationError({"properties": _format_pydantic_errors(exc)})
        except IntegrityError:
            raise Conflict("An account with this external_id already exists for this team.")
        return account


def _format_pydantic_errors(exc: PydanticValidationError) -> list[str]:
    messages = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"])
        messages.append(f"{loc}: {err['msg']}" if loc else err["msg"])
    return messages


class AccountOrganizationMemberSerializer(serializers.ModelSerializer):
    """Slim organization-member representation for Customer analytics account rows."""

    user = UserBasicSerializer(
        read_only=True,
        help_text="Basic profile of the member's user (uuid, distinct_id, first_name, last_name, email).",
    )

    class Meta:
        model = OrganizationMembership
        fields = ["id", "user"]
        read_only_fields = ["id", "user"]
        extra_kwargs = {"id": {"help_text": "Organization membership ID."}}


class AccountNotebookSerializer(serializers.ModelSerializer):
    created_by = UserBasicSerializer(read_only=True)
    last_modified_by = UserBasicSerializer(read_only=True)
    title = serializers.CharField(
        max_length=256,
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="Human-readable title of the account notebook.",
    )
    content = serializers.JSONField(
        required=False,
        allow_null=True,
        help_text="Notebook content as a ProseMirror JSON document structure.",
    )
    text_content = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="Plain text representation of the notebook content for search.",
    )

    class Meta:
        model = Notebook
        fields = [
            "id",
            "short_id",
            "title",
            "content",
            "text_content",
            "created_at",
            "created_by",
            "last_modified_at",
            "last_modified_by",
        ]
        read_only_fields = [
            "id",
            "short_id",
            "created_at",
            "created_by",
            "last_modified_at",
            "last_modified_by",
        ]


class CustomPropertyDefinitionSerializer(serializers.ModelSerializer):
    """A team-scoped definition of a custom account property — the attribute side of the model.

    Holds only the property's shape (name, display type, big-number flag). Per-account values are
    stored separately, so this serializer never reads or writes account values.
    """

    name = serializers.CharField(
        max_length=400,
        help_text="Human-readable name of the custom property. Unique within the team.",
    )
    description = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        help_text="Optional description of what the property represents.",
    )
    display_type = serializers.ChoiceField(
        choices=[t.value for t in DisplayType],
        help_text=(
            "How the property is interpreted and rendered: 'text', 'number', 'currency', "
            "'percent', 'date', 'datetime', or 'boolean'."
        ),
    )
    is_big_number = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Abbreviate large numbers (e.g. 10,000 → 10K). Only applies to numeric properties.",
    )

    class Meta:
        model = CustomPropertyDefinition
        fields = [
            "id",
            "name",
            "description",
            "display_type",
            "is_big_number",
            "created_at",
            "created_by",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "created_by", "updated_at"]

    def validate(self, attrs):
        display_type = attrs.get("display_type") or getattr(self.instance, "display_type", None)
        is_big_number = attrs.get("is_big_number")
        if is_big_number is None:
            is_big_number = getattr(self.instance, "is_big_number", False)

        if display_type and is_big_number and DATA_TYPE_BY_DISPLAY_TYPE[DisplayType(display_type)] != DataType.NUMERIC:
            attrs["is_big_number"] = False

        return attrs

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        validated_data["team_id"] = self.context["team_id"]
        try:
            return super().create(validated_data)
        except IntegrityError:
            raise Conflict("A custom property with this name already exists for this team.")

    def update(self, instance, validated_data):
        try:
            return super().update(instance, validated_data)
        except IntegrityError:
            raise Conflict("A custom property with this name already exists for this team.")


@extend_schema_field({"oneOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}]})
class CustomPropertyValueField(serializers.Field):
    """A custom property value — a JSON scalar (string, number, or boolean).

    Datetimes are sent and returned as ISO-8601 strings. The concrete type a property accepts is
    set by its definition and validated server-side.
    """

    def to_internal_value(self, data):
        if data is None or isinstance(data, dict | list):
            raise serializers.ValidationError("Value must be a string, number, or boolean.")
        return data

    def to_representation(self, value):
        return value


class CustomPropertyValueWriteSerializer(serializers.Serializer):
    definition = serializers.UUIDField(
        help_text="UUID of the custom property definition whose value to set for this account."
    )
    value = CustomPropertyValueField(
        help_text=(
            "Value to store, matching the definition's type: a number for number/currency/percent, a "
            "boolean for boolean, an ISO-8601 string for date/datetime, or text for text properties."
        )
    )


class CustomPropertyValueSerializer(serializers.Serializer):
    """An account's current value for a custom property (read shape)."""

    id = serializers.UUIDField(read_only=True, help_text="Unique id of this value record.")
    account_id = serializers.UUIDField(read_only=True, help_text="Account the value belongs to.")
    definition_id = serializers.UUIDField(read_only=True, help_text="Custom property definition the value is for.")
    value = CustomPropertyValueField(read_only=True, help_text="The stored value, typed per the property's data type.")
    data_type = serializers.CharField(
        read_only=True, help_text="The property's data category: 'string', 'numeric', 'boolean', or 'datetime'."
    )
    created_at = serializers.DateTimeField(read_only=True, help_text="When this value was set.")
    created_by_id = serializers.IntegerField(
        read_only=True, allow_null=True, help_text="Id of the user who set this value, if known."
    )
