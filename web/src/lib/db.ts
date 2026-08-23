// Project-specific aliases over the generated schema types.
// Kept out of database.types.ts so `npm run gen:types` can overwrite that file
// wholesale without a hand-merge.

import type { Tables, Enums } from './database.types'
import { Constants } from './database.types'

export type Article = Tables<'articles'>
export type ArticleCredit = Tables<'article_credits'>
export type GalleryImage = Tables<'images'>
export type Profile = Tables<'profiles'>
export type Reaction = Tables<'reactions'>
export type ListRow = Tables<'lists'>
export type ListImage = Tables<'list_images'>

export type ArticleCategory = Enums<'article_category'>
export type ReactionType = Enums<'reaction_type'>

/** The five Style categories, in the order the filter chips render them. */
export const ARTICLE_CATEGORIES = Constants.public.Enums.article_category

/** Category → the mono code shown on each grid tile (from the prototype). */
export const CATEGORY_CODE: Record<ArticleCategory, string> = {
  grooming: 'GRM',
  item: 'ITM',
  news: 'NWS',
  pictorial: 'PIC',
  sneakers: 'SNK',
}
