
---

## 3. `/docs/edge-case-checklist.md`

```markdown
# Edge Case Discovery Checklist

A systematic checklist for identifying edge cases and boundary conditions in any feature.

---

## Input Validation

### Empty & Null Values
- [ ] Null/undefined input
- [ ] Empty string (`""`)
- [ ] Whitespace only (`"   "`)
- [ ] Zero (`0`)
- [ ] Empty array (`[]`)
- [ ] Empty object (`{}`)

### String Validation
- [ ] Leading spaces
- [ ] Trailing spaces
- [ ] Multiple consecutive spaces
- [ ] Special characters: `!@#$%^&*()_+-={}[]|:";'<>?,./`
- [ ] Unicode characters
- [ ] Emoji in text fields 😀
- [ ] Line breaks and tabs (`\n`, `\t`)
- [ ] HTML tags in input
- [ ] Extremely long strings (>1000 chars)

### Numeric Validation
- [ ] Negative numbers
- [ ] Zero
- [ ] Decimal values
- [ ] Very large numbers (overflow)
- [ ] Very small numbers (underflow)
- [ ] Scientific notation
- [ ] Infinity
- [ ] NaN (Not a Number)
- [ ] Boundary values (min, min-1, max, max+1)

### Data Type Mismatches
- [ ] String where number expected
- [ ] Number where string expected
- [ ] Boolean as string ("true" vs true)
- [ ] Date format variations
- [ ] JSON parsing errors

---

## Security Vulnerabilities

### Injection Attacks
- [ ] SQL Injection: `' OR '1'='1`
- [ ] SQL Injection: `'; DROP TABLE users--`
- [ ] XSS: `<script>alert('xss')</script>`
- [ ] XSS: `<img src=x onerror=alert('xss')>`
- [ ] Command Injection: `; rm -rf /`
- [ ] LDAP Injection
- [ ] XML Injection

### Authentication & Authorization
- [ ] Access without authentication
- [ ] Access with expired token
- [ ] Access with invalid token
- [ ] Privilege escalation attempts
- [ ] CSRF token validation
- [ ] Session fixation
- [ ] Concurrent sessions

### Data Exposure
- [ ] Sensitive data in URLs
- [ ] Sensitive data in logs
- [ ] Unencrypted data transmission
- [ ] Missing CORS headers
- [ ] Exposed API keys

---

## State & Timing

### Concurrent Operations
- [ ] Multiple users editing same record
- [ ] Race conditions
- [ ] Deadlocks
- [ ] Simultaneous submissions
- [ ] Parallel API calls

### Session Management
- [ ] Session timeout
- [ ] Expired tokens
- [ ] Token refresh
- [ ] Logout while operation in progress
- [ ] Multiple tabs/windows
- [ ] Cross-device sessions

### Network Conditions
- [ ] Network disconnection mid-operation
- [ ] Slow network (3G simulation)
- [ ] Request timeout
- [ ] API rate limiting
- [ ] 502/503 errors
- [ ] DNS failures

### Timing Issues
- [ ] Operations in wrong order
- [ ] Rapid repeated clicks
- [ ] Delayed responses
- [ ] Retry mechanisms
- [ ] Cache staleness

---

## Data & Scale

### Data Volume
- [ ] Empty dataset (0 records)
- [ ] Single record
- [ ] Large dataset (10,000+ records)
- [ ] Pagination edge cases
- [ ] Maximum page size
- [ ] Extremely large single record

### Data Integrity
- [ ] Duplicate entries
- [ ] Missing foreign keys
- [ ] Orphaned records
- [ ] Circular references
- [ ] Data type mismatches in DB
- [ ] Character encoding issues

### Performance
- [ ] Response time with large data
- [ ] Memory usage with large files
- [ ] Database query performance
- [ ] API rate limits
- [ ] Concurrent user load
- [ ] Memory leaks

---

## User Behavior

### Navigation
- [ ] Browser back button
- [ ] Browser forward button
- [ ] Page refresh mid-operation
- [ ] Navigate away during save
- [ ] Bookmarking dynamic pages
- [ ] Deep linking

### Form Interactions
- [ ] Submit without filling required fields
- [ ] Double-click submit button
- [ ] Submit with Enter key
- [ ] Autofill behavior
- [ ] Copy-paste formatted text
- [ ] Browser autocomplete

### Browser Features
- [ ] Disabled JavaScript
- [ ] Disabled cookies
- [ ] Private/Incognito mode
- [ ] Ad blockers
- [ ] Browser extensions
- [ ] Developer tools open

---

## Platform & Environment

### Browsers
- [ ] Chrome (latest 2 versions)
- [ ] Firefox (latest 2 versions)
- [ ] Safari (latest 2 versions)
- [ ] Edge (latest 2 versions)
- [ ] Mobile Safari (iOS)
- [ ] Chrome Mobile (Android)
- [ ] Older browser versions

### Devices
- [ ] Desktop (1920x1080)
- [ ] Laptop (1366x768)
- [ ] Tablet (iPad, Android tablet)
- [ ] Mobile (iPhone, Android phone)
- [ ] Ultra-wide monitors
- [ ] Small screens (<375px width)

### Operating Systems
- [ ] Windows 10/11
- [ ] macOS
- [ ] Linux
- [ ] iOS (latest 2 versions)
- [ ] Android (latest 2 versions)

### Network
- [ ] WiFi
- [ ] 4G/LTE
- [ ] 3G
- [ ] Offline mode
- [ ] VPN connections
- [ ] Proxy servers

---

## Integration Points

### Third-party APIs
- [ ] API returns 500 error
- [ ] API timeout
- [ ] API rate limit exceeded
- [ ] API returns unexpected format
- [ ] API authentication fails
- [ ] API deprecated endpoints

### Database
- [ ] Connection pool exhausted
- [ ] Database timeout
- [ ] Transaction rollback
- [ ] Deadlock detection
- [ ] Replication lag
- [ ] Backup/restore in progress

### File Operations
- [ ] File doesn't exist
- [ ] File permissions denied
- [ ] Disk full
- [ ] File locked by another process
- [ ] Unsupported file format
- [ ] Corrupted file

---

## Accessibility

### Keyboard Navigation
- [ ] Tab order logical
- [ ] All features accessible via keyboard
- [ ] Escape key behavior
- [ ] Enter key submits forms
- [ ] Focus indicators visible
- [ ] Skip navigation links

### Screen Readers
- [ ] ARIA labels present
- [ ] Alt text for images
- [ ] Form labels associated
- [ ] Error messages announced
- [ ] Dynamic content updates announced

### Visual
- [ ] Color contrast (WCAG AA)
- [ ] Text resizing up to 200%
- [ ] Works without color alone
- [ ] Focus indicators 3:1 contrast
- [ ] Readable fonts

---

## Internationalization (i18n)

### Language & Locale
- [ ] Right-to-left languages (Arabic, Hebrew)
- [ ] Character sets (UTF-8, UTF-16)
- [ ] Date format variations
- [ ] Currency symbols
- [ ] Number formatting (1,000 vs 1.000)
- [ ] Time zones

### Text Length
- [ ] Short labels in German (long words)
- [ ] Translation text overflow
- [ ] Multi-byte characters
- [ ] Special characters in URLs

---

## Usage Template

For each feature, systematically go through relevant sections:

1. **Identify applicable categories** (not all apply to every feature)
2. **Check each item** in those categories
3. **Document discovered edge cases** in test plan
4. **Prioritize based on risk** and likelihood
5. **Create test cases** for high-priority edges

---

*Use this checklist as a starting point. Adapt based on your specific domain and requirements.*