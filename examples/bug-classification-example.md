
---

## 2. `/examples/bug-classification-example.md`

```markdown
# Example: Bug Classification & Analysis

## Reported Bug
**Title**: Application crashes when user uploads file larger than 10MB  
**Reporter**: john.doe@bosch.com  
**Environment**: Production, Chrome 120, Windows 11

---

## Bug Analysis: File Upload Crash

**Bug ID**: BUG-2024-0156  
**Severity**: **Critical**  
**Priority**: **P0**  
**Category**: Functional - File Upload

---

### Classification Rationale

This is classified as **Critical/P0** because:

1. **Complete System Failure**: The application crashes entirely, not just the upload feature. Users lose all unsaved work across the entire session.

2. **Core Feature Impact**: File upload is used by 40% of daily active users. The 10MB limit is easily exceeded with common business files (presentations, high-res images, compiled reports).

3. **Data Loss Risk**: Users working on multi-step forms with file attachments lose all entered data when the crash occurs.

---

### Impact Analysis

**User Experience Impact**: **Severe**
- Complete workflow interruption
- Loss of work in progress
- Requires app restart
- No warning or graceful degradation

**Business Impact**: **High**
- Estimated 500+ users affected daily
- Support ticket surge (already 23 tickets in 2 days)
- SLA breach for Enterprise customers
- Potential contract penalties

**Frequency**: **High**
- Occurs every time file >10MB is uploaded
- Common file types affected: .pptx, .pdf, .zip, .mp4
- 30% of upload attempts exceed 10MB

**Reproducibility**: **Always (100%)**
- Deterministic on all browsers
- Affects all users regardless of role
- Occurs in all environments (dev, staging, prod)

---

### Reproduction Steps

1. Navigate to `/documents/upload`
2. Click "Choose File" button
3. Select any file >10MB (tested with 15MB PDF)
4. Click "Upload" button
5. **Actual Result**: Application crashes immediately, white screen, requires refresh
6. **Expected Result**: Display error "File size exceeds 10MB limit" without crash

**Test Files Used:**
- `large-presentation.pptx` (12MB) ✓ Reproduces
- `high-res-photo.jpg` (11MB) ✓ Reproduces
- `video-clip.mp4` (25MB) ✓ Reproduces
- `normal-doc.pdf` (5MB) ✗ Works fine

---

### Root Cause Hypothesis

Likely causes to investigate:

1. **Client-side memory overflow**: Attempting to load entire file into browser memory before validation
2. **Missing size validation**: No client-side check before upload starts
3. **Unhandled exception**: Server returns error but no error boundary catches it
4. **Buffer overflow**: Upload buffer configured for max 10MB

---

### Additional Investigation Needed

**Technical:**
- [ ] Is there any client-side file size validation?
- [ ] What is the configured server-side max upload size?
- [ ] Are error boundaries implemented around upload component?
- [ ] What error does the server return (if any)?
- [ ] Browser console errors/stack trace?

**Business:**
- [ ] What should the actual file size limit be?
- [ ] How many users are affected (analytics)?
- [ ] Are there enterprise customers blocked?
- [ ] What's the business justification for 10MB limit?

**Environment:**
- [ ] Does this occur on mobile browsers?
- [ ] Same behavior on slow network connections?
- [ ] Any difference between file types?

---

### Recommended Action

#### Immediate (Within 24 hours) - P0

**Emergency Hotfix:**
```javascript
// Add client-side validation before upload
if (file.size > 10 * 1024 * 1024) {
  showError("File size must be less than 10MB");
  return;
}

Error Boundary:
// Add error boundary to prevent full app crash
<ErrorBoundary fallback={<UploadError />}>
  <FileUpload />
</ErrorBoundary>

Short-term (Within 1 week) - P1
Proper Error Handling:

Server-side validation with 413 status code
User-friendly error messages
Maintain application state after error
Testing:

Add automated tests for file size boundaries
Test files: 9.9MB, 10MB, 10.1MB, 50MB, 100MB
Cross-browser testing
Network interruption scenarios
Monitoring:

Log all upload attempts with file sizes
Alert on upload failures
Track error rates
Long-term (Within 1 month) - P2
UX Improvements:

Display file size limit prominently
Show file size before upload
Progress bar for uploads
Consider file compression suggestions
Architecture Review:

Evaluate chunked upload for large files
Implement resumable uploads
Review memory management
Consider CDN/S3 for large files
Prevention:

Add to regression test suite
Document file size limits in API spec
Code review checklist for upload features
Related Test Cases
Existing (Should have caught this):

❌ TC-Upload-001: File size validation (apparently not comprehensive)
Missing (Need to create):

TC-Upload-Edge-001: Upload exactly 10MB file
TC-Upload-Edge-002: Upload 10.1MB file (just over limit)
TC-Upload-Edge-003: Upload 100MB file (way over limit)
TC-Upload-Edge-004: Network failure during upload
TC-Upload-Edge-005: Multiple concurrent uploads
TC-Upload-Edge-006: Different file types at size limit
Bug Severity Reference
Level	This Bug	Why?
Critical ✓	YES	Complete app crash + data loss
Major	No	Would be major if only upload failed
Minor	No	Way more severe than minor
Trivial	No	Not cosmetic
Priority Justification
Factor	Assessment	Weight
User Impact	Severe (crash + data loss)	🔴 High
Frequency	High (30% of uploads)	🔴 High
Business Impact	Contract penalties possible	🔴 High
Workaround	Manual file compression (difficult)	🔴 High
Result	P0 - Fix Immediately	
Verification Steps (Post-Fix)
After fix is deployed:

✅ Upload 15MB file → Should show error without crash
✅ Upload 9.5MB file → Should work normally
✅ Upload exactly 10MB file → Verify boundary handling
✅ Check error message clarity
✅ Verify application state maintained
✅ Test across Chrome, Firefox, Safari, Edge
✅ Verify monitoring/logging works
✅ Check analytics for error rate drop
