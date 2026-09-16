"""
Schema.org JSON-LD Pydantic models for digital collection items.

All metadata is serialized as JSON-LD using schema.org vocabulary.
The ARK is stored as both `@id` and the `ark` property.
The `argonath:fieldSources` map records per-field provenance.

Field source values: 'ai' | 'human' | 'ai-approved' | 'hybrid'
"""
from typing import Literal
from pydantic import BaseModel, Field


ARGONATH_NS = "https://lib.ucdavis.edu/argonath/ns#"
SCHEMA_CONTEXT = {
    "@vocab": "https://schema.org/",
    "argonath": ARGONATH_NS,
}

FieldSource = Literal["ai", "human", "ai-approved", "hybrid"]


class Identifier(BaseModel):
    """A schema.org PropertyValue used as an identifier entry.

    :param str type: Always 'PropertyValue'.
    :param str property_id: Identifier type (e.g. 'ARK', 'box', 'folder').
    :param str value: Identifier value.
    """
    type: str = Field("PropertyValue", alias="@type")
    property_id: str = Field(..., alias="propertyID")
    value: str

    model_config = {"populate_by_name": True}


class ItemMetadata(BaseModel):
    """Schema.org metadata for a single digital collection item.

    Serializes to JSON-LD. The `field_sources` map records which fields
    were set by AI vs human review.

    :param str id: ARK identifier (e.g. 'ark:/87287/d7abc1234').
    :param str ark: Duplicate of id for easy property lookup.
    :param str type: Schema.org type (e.g. 'Photograph', 'Book', 'Map').
    :param str name: Item title.
    :param str description: Item description.
    :param str creator: Item creator/author.
    :param str date_created: Creation date (ISO 8601 or year string), or an
        XSD-typed literal dict — see digtk.lib.dc_mapping.typed_date_literal().
    :param str|dict license: License/rights statement — a plain string, or
        {"@id": uri} for a URI-shaped rights statement.
    :param list[dict] about: Subject entries, each {"@id": value}.
    :param str publisher: Item publisher.
    :param list identifiers: Additional identifiers (box, folder, etc.).
    :param dict field_sources: Per-field provenance map.
    """
    id: str = Field(..., alias="@id")
    ark: str
    type: str = Field("CreativeWork", alias="@type")
    name: str | None = None
    description: str | None = None
    creator: str | None = None
    date_created: str | dict | None = Field(None, alias="dateCreated")
    license: str | dict | None = None
    about: list[dict] = Field(default_factory=list)
    publisher: str | None = None
    identifiers: list[Identifier] = Field(default_factory=list, alias="identifier")
    field_sources: dict[str, FieldSource] = Field(
        default_factory=dict, alias="argonath:fieldSources"
    )

    model_config = {"populate_by_name": True}

    def to_jsonld(self):
        """Serialize to a JSON-LD dict with full context.

        Only fields that are actually set are included (besides the always-
        present @context/@id/ark/@type) — an absent field is omitted rather
        than written as null, so a consumer merging this into an existing
        sidecar (see digtk.models.digital_website.pipeline.merge_dc_metadata())
        never clobbers a value some other writer already set with a null.

        :returns: JSON-LD document dict ready for serialization.
        :rtype: dict
        """
        doc = {"@context": SCHEMA_CONTEXT, "@id": self.id, "ark": self.ark, "@type": self.type}
        for field, alias in (
            ("name", "name"), ("description", "description"), ("creator", "creator"),
            ("date_created", "dateCreated"), ("license", "license"), ("publisher", "publisher"),
        ):
            value = getattr(self, field)
            if value is not None:
                doc[alias] = value
        if self.about:
            doc["about"] = self.about
        if self.identifiers:
            doc["identifier"] = [i.model_dump(by_alias=True) for i in self.identifiers]
        if self.field_sources:
            doc["argonath:fieldSources"] = self.field_sources
        return doc

    @classmethod
    def from_jsonld(cls, doc):
        """Deserialize from a JSON-LD dict.

        :param dict doc: Parsed JSON-LD document — see to_jsonld() for shape.
        :returns: ItemMetadata instance.
        :rtype: ItemMetadata
        """
        return cls(
            id=doc["@id"],
            ark=doc.get("ark", doc["@id"]),
            type=doc.get("@type", "CreativeWork"),
            name=doc.get("name"),
            description=doc.get("description"),
            creator=doc.get("creator"),
            date_created=doc.get("dateCreated"),
            license=doc.get("license"),
            about=doc.get("about", []),
            publisher=doc.get("publisher"),
            identifiers=[Identifier.model_validate(i) for i in doc.get("identifier", [])],
            field_sources=doc.get("argonath:fieldSources", {}),
        )
