const ACTION_TAG_DELIMITER = ','

export function parseActionTags(value?: string | null): string[] {
  if (!value) return []
  return value
    .split(ACTION_TAG_DELIMITER)
    .map(tag => tag.trim())
    .filter(Boolean)
}

export function serializeActionTags(tags: string[]): string {
  return [...new Set(tags.map(tag => tag.trim()).filter(Boolean))].join(ACTION_TAG_DELIMITER)
}

export function hasActionTag(value: string | null | undefined, tag: string): boolean {
  return parseActionTags(value).includes(tag)
}

