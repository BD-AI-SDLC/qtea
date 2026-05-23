
---

## 5. `/docs/templates/bug-report-template.md`

```markdown
# Bug Report Analysis Template

Use this template when analyzing and classifying bug reports.

---

## Bug Report Analysis: [Bug ID/Title]

**Bug ID**: [BUG-XXXX]  
**Reported By**: [Name/Email]  
**Reported Date**: [Date]  
**Environment**: [Browser/OS/Version]

---

### Classification

**Severity**: [Critical | Major | Minor | Trivial]  
**Priority**: [P0 | P1 | P2 | P3]  
**Category**: [Functional | Performance | Security | UI/UX | Data]  
**Component**: [Which part of system]

---

### Classification Rationale

**Why this severity/priority?**

[2-3 sentences explaining the classification based on:]
- User impact
- Business criticality
- Frequency of occurrence
- Workaround availability

---

### Impact Analysis

**User Experience Impact**: [High | Medium | Low]

[Describe how this affects end users]

**Business Impact**: [High | Medium | Low]

[Describe business consequences - revenue, reputation, compliance, etc.]

**Frequency**: [Always | Often | Sometimes | Rare]

[How often does this occur?]

**Reproducibility**: [Always | Sometimes | Random]

[Can it be consistently reproduced?]

---

### Bug Details

**Actual Behavior:**
[What actually happens]

**Expected Behavior:**
[What should happen]

**Reproduction Steps:**
1. [Step 1]
2. [Step 2]
3. [Step 3]
4. **Result**: [Actual outcome]

**Test Data Used:**
- [Data point 1]
- [Data point 2]

---

### Root Cause Analysis

**Hypothesis:**
[Your theory about what's causing the bug]

**Likely Cause:**
- [ ] Code defect
- [ ] Configuration issue
- [ ] Data issue
- [ ] Integration failure
- [ ] Environmental issue

---

### Additional Investigation Needed

**Technical Questions:**
- [ ] [Question 1]
- [ ] [Question 2]

**Business Questions:**
- [ ] [Question 1]
- [ ] [Question 2]

**Environment Questions:**
- [ ] Does this occur in all environments?
- [ ] Does this occur on all browsers/devices?
- [ ] Are there specific conditions required?

---

### Recommended Action

**Immediate (P0/P1):**
- [Action 1]
- [Action 2]

**Short-term (1 week):**
- [Action 1]
- [Action 2]

**Long-term (1 month):**
- [Preventive measure 1]
- [Preventive measure 2]

---

### Related Test Cases

**Existing Test Cases (that should have caught this):**
- [TC-XXX]: [Description]
- [TC-YYY]: [Description]

**Missing Test Cases (need to create):**
- [New TC-1]: [Description]
- [New TC-2]: [Description]

---

### Attachments

- [ ] Screenshots
- [ ] Screen recording
- [ ] Browser console logs
- [ ] Network logs
- [ ] Server logs
- [ ] Stack trace

---

### Verification Steps (Post-Fix)

After fix is deployed, verify:

1. [ ] [Verification step 1]
2. [ ] [Verification step 2]
3. [ ] [Regression test: related features still work]
4. [ ] [Performance: no degradation]

---

### Severity Quick Reference

| Level | Definition | This Bug? |
|-------|------------|-----------|
| **Critical** | System crash, data loss, security breach | [ ] |
| **Major** | Core feature broken, difficult workaround | [ ] |
| **Minor** | Feature works with issues, easy workaround | [ ] |
| **Trivial** | Cosmetic, no functional impact | [ ] |

---

*Analysis Date: [Date]*  
*Analyzer: [Your Name]*
