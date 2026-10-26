"""Render contract schema fields without maintaining another prompt field list."""
import json


def schema_example(schema):
    if 'enum' in schema:
        return schema['enum'][0] if schema['enum'] else None
    kind=schema.get('type')
    if isinstance(kind,list):
        kind=next((x for x in kind if x!='null'),'null')
    if kind=='object':
        return {key:schema_example(value) for key,value in schema.get('properties',{}).items()}
    if kind=='array':
        return []
    if kind=='boolean':
        return True
    if kind in {'number','integer'}:
        return schema.get('minimum',0)
    if kind=='null':
        return None
    return ''


def schema_prompt_fields(schema, *, descriptions, order=(), required_only=False,
                         enum_separator='|', boolean='true or false', enum_separators=None,
                         separate_enums=()):
    """Descriptions/order annotate existing fields; neither can add a field."""
    fields=schema['properties']
    keys=list(schema.get('required',fields)) if required_only else list(fields)
    keys=[key for key in order if key in keys]+[key for key in keys if key not in order]
    result=[]
    for key in keys:
        field=fields[key]
        if key in descriptions:
            value=json.dumps(descriptions[key],ensure_ascii=False)
        elif 'enum' in field:
            separator=(enum_separators or {}).get(key,enum_separator)
            if separator=='|' and key not in separate_enums:
                value=json.dumps('|'.join(str(x) for x in field['enum']),ensure_ascii=False)
            else:
                value=separator.join(json.dumps(x,ensure_ascii=False) for x in field['enum'])
        elif field.get('type')=='boolean':
            value=boolean
        else:
            default=schema_example(field)
            value='0.0' if field.get('type')=='number' and default==0 else json.dumps(default,ensure_ascii=False)
        result.append(json.dumps(key)+':'+value)
    return result
