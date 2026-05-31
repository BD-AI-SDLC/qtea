# Example: Login Feature Test Strategy

## Input Requirement
> "Users should be able to log in using email and password. Add 'Remember Me' checkbox."

---

## Test Strategy: User Authentication

### Scope

**In scope:** Email/password login, "Remember Me" persistent session, input validation, error handling, security (injection, brute force).

**Out of scope:** Registration, password reset, SSO/OAuth, admin login.

### Test Cases

#### TC-001: Successful Login - Valid Credentials
- **Type**: UI + API
- **Priority**: P0
- **Preconditions**: 
  - User account exists with email: `test.user@example.com`
  - Database is accessible
- **Steps**: 
  1. Navigate to `/login`
  2. Enter email: `test.user@example.com`
  3. Enter password: `ValidP@ss123`
  4. Click "Login" button
- **Expected Result**: 
  - Redirect to `/dashboard`
  - Session cookie created
  - API returns 200 with auth token
  - User profile loaded

#### TC-002: Failed Login - Invalid Password
- **Type**: UI + API
- **Priority**: P0
- **Preconditions**: User account exists
- **Steps**: 
  1. Navigate to `/login`
  2. Enter valid email: `test.user@example.com`
  3. Enter wrong password: `WrongPassword123`
  4. Click "Login"
- **Expected Result**: 
  - Error message: "Invalid email or password"
  - Remain on login page
  - API returns 401 Unauthorized
  - Failed attempt logged

#### TC-003: Remember Me Functionality
- **Type**: UI + Session Management
- **Priority**: P1
- **Preconditions**: Valid user credentials
- **Steps**: 
  1. Navigate to `/login`
  2. Enter valid credentials
  3. Check "Remember Me" checkbox
  4. Click "Login"
  5. Close browser completely
  6. Reopen and navigate to app URL
- **Expected Result**: 
  - User auto-logged in
  - 30-day persistent token active
  - No re-authentication required

#### TC-004: Empty Field Validation
- **Type**: UI Validation
- **Priority**: P1
- **Steps**: 
  1. Navigate to `/login`
  2. Leave both fields empty
  3. Click "Login"
- **Expected Result**: 
  - Error: "Email and password are required"
  - No API call made
  - Form doesn't submit

#### TC-005: Unregistered Email
- **Type**: UI + API
- **Priority**: P1
- **Steps**: 
  1. Enter unregistered email: `nobody@example.com`
  2. Enter any password
  3. Click "Login"
- **Expected Result**: 
  - Generic error: "Invalid email or password"
  - API returns 401
  - Don't reveal which field is wrong (security)

#### TC-006: Network Interruption During Login
- **Type**: Error Handling
- **Priority**: P1
- **Steps**: 
  1. Enter valid credentials
  2. Click "Login"
  3. Simulate network disconnection during API call
- **Expected Result**: 
  - Error message: "Connection lost. Please try again."
  - Retry option available
  - No partial auth state

#### TC-007: SQL Injection Attempt
- **Type**: Security
- **Priority**: P0
- **Steps**:
  1. Navigate to `/login`
  2. Enter email: `' OR '1'='1`
  3. Enter password: `' OR '1'='1`
  4. Click "Login"
- **Expected Result**:
  - Login rejected
  - Input sanitized server-side
  - No database error exposed

#### TC-008: Brute Force Rate Limiting
- **Type**: Security
- **Priority**: P0
- **Steps**:
  1. Navigate to `/login`
  2. Submit 5 consecutive login attempts with wrong password
  3. Attempt a 6th login
- **Expected Result**:
  - Account temporarily locked or CAPTCHA displayed
  - Rate limit response (429) after threshold

#### TC-009: Email With Special Characters
- **Type**: UI + API
- **Priority**: P2
- **Steps**:
  1. Navigate to `/login`
  2. Enter email: `user+tag@example.com`
  3. Enter valid password
  4. Click "Login"
- **Expected Result**:
  - Login succeeds if account exists
  - Email with `+` sign handled correctly

#### TC-010: Password At Max Length
- **Type**: UI + API
- **Priority**: P2
- **Steps**:
  1. Navigate to `/login`
  2. Enter valid email
  3. Enter password at max allowed length (255 chars)
  4. Click "Login"
- **Expected Result**:
  - Login succeeds if credentials are valid
  - No truncation or server error

#### TC-011: Whitespace-Only Input
- **Type**: UI Validation
- **Priority**: P2
- **Steps**:
  1. Navigate to `/login`
  2. Enter spaces-only in both fields
  3. Click "Login"
- **Expected Result**:
  - Treated as empty input
  - Validation error shown
  - No API call made
