import { describe, it, expect, beforeEach } from 'vitest'
import { token, currentUser, isAuthed, setToken, logout } from './auth'

describe('auth store', () => {
  beforeEach(() => {
    token.value = null
    currentUser.value = null
    localStorage.clear()
  })

  it('isAuthed is false when no token or user', () => {
    expect(isAuthed.value).toBe(false)
  })

  it('setToken persists to localStorage', () => {
    setToken('abc')
    expect(token.value).toBe('abc')
    expect(localStorage.getItem('sh_token')).toBe('abc')
  })

  it('isAuthed is true when both token and user are set', () => {
    setToken('tok')
    currentUser.value = { user_id: 'u1', username: 'a', display_name: 'A', is_admin: false, picture_url: null, picture_hash: null, bio: null, is_new_member: false }
    expect(isAuthed.value).toBe(true)
  })

  it('logout clears everything', () => {
    setToken('tok')
    currentUser.value = { user_id: 'u1', username: 'a', display_name: 'A', is_admin: false, picture_url: null, picture_hash: null, bio: null, is_new_member: false }
    logout()
    expect(token.value).toBe(null)
    expect(currentUser.value).toBe(null)
    expect(localStorage.getItem('sh_token')).toBe(null)
  })
})
