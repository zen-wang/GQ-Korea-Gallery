import { createClient } from '@supabase/supabase-js'
import type { Database } from './database.types'

const url = import.meta.env.VITE_SUPABASE_URL
const publishableKey = import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY

if (!url || !publishableKey) {
  throw new Error(
    'Missing VITE_SUPABASE_URL / VITE_SUPABASE_PUBLISHABLE_KEY — copy web/.env.example to web/.env.local and fill both in.',
  )
}

// This key is public: it ships inside the deployed bundle. Everything that
// keeps the gallery private is enforced server-side by RLS
// (supabase/migrations/*_rls_policies.sql), never here.
export const supabase = createClient<Database>(url, publishableKey)
