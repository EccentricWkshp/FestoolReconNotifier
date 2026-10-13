# Timezone Feature Guide

The Festool Recon Monitor now supports local timezone display for all timestamps!

## 🌍 What Changed

### Before:
- All times displayed in UTC (Universal Time)
- Times like "2025-10-12 05:46:24" were confusing
- Didn't match your local system time

### After:
- **Local System Time** (default) - matches your computer's timezone
- **UTC** - optional for international users
- User-friendly formats: "Oct 12, 3:45 PM" or "2 hours ago"
- Automatic conversion of existing data

## ⚙️ How to Use

### Change Timezone Mode

1. Open **Settings** button
2. Go to **Monitoring** tab
3. Find **"Time Display Mode"** section
4. Choose:
   - **Local System Time** (recommended) - matches your location
   - **UTC (Universal Time)** - for coordinating across timezones
5. Click **Save**

### One-Time Conversion Prompt

If you have existing data and switch from UTC to Local:

```
Convert Database Times?

You've switched from UTC to Local time.

Would you like to convert all existing timestamps 
from UTC to your local timezone?

This is recommended for accurate time display.

[Yes] [No]
```

**Recommendation**: Click **Yes** to convert all historical data.

## 📅 Time Display Formats

### Full Format
Used in "Current Product" tab and exports:
- **Local**: "2025-10-12 09:46:24 AM"
- **UTC**: "2025-10-12 16:46:24" (24-hour format)

### Short Format
Used in History table:
- **Local**: "Oct 12, 9:46 AM"
- **UTC**: "Oct 12, 16:46"

### Relative Time
Shown in Current Product tab:
- "5 minutes ago"
- "2 hours ago"
- "just now"

### Duration Format
For "Minutes Live" column:
- "45m" (45 minutes)
- "2h 15m" (2 hours 15 minutes)
- "5h" (5 hours)

## 🔄 Database Conversion

### What Gets Converted

When switching from UTC to Local, these timestamps are updated:
- ✅ Product posted times
- ✅ Product replaced times
- ✅ User last login times
- ✅ Notification sent times

### How It Works

1. Assumes existing naive datetimes are in UTC
2. Converts each to your local timezone
3. Stores as naive local datetime
4. All future times use local timezone

### Safety

- ✅ Non-destructive - can revert by switching back to UTC
- ✅ Logged - check `festool_monitor.log` for confirmation
- ✅ Tested - conversions are accurate

## 📊 Examples

### Before Conversion (UTC)
```
Product: Router OF 1010 REBQ-Plus
Posted: 2025-10-12 13:46:24
```

### After Conversion (Pacific Time, UTC-7)
```
Product: Router OF 1010 REBQ-Plus
Posted: Oct 12, 6:46 AM (5 hours ago)
```

## 🎯 Recommended Settings

### For Most Users:
- **Mode**: Local System Time ✅
- **Convert existing data**: Yes ✅

### For International Teams:
- **Mode**: UTC
- Keep all timestamps in universal time for coordination

### For Developers:
- **Mode**: UTC
- Easier to debug with consistent timezone

## ⚡ Quick Tips

### Check Your Current Mode
1. Open Settings → Monitoring tab
2. Look at "Timezone" dropdown
3. Current selection is your active mode

### Timestamps Look Wrong?
1. Verify timezone mode matches your preference
2. If recently switched, ensure you converted the data
3. Check your system time is correct

### Export Data
When exporting to CSV/Excel:
- Times export in the format you're currently viewing
- Local mode → local times in export
- UTC mode → UTC times in export

## 🔧 Technical Details

### Storage Format
- Datetimes stored as **naive** (no timezone info in database)
- Interpreted as UTC or Local based on `timezone_mode` setting
- Converted for display using Python's `datetime.astimezone()`

### Performance
- ✅ No impact - conversion only happens on display
- ✅ Fast - uses optimized datetime functions
- ✅ Efficient - timezone manager caches settings

### Compatibility
- Works on Windows, macOS, Linux
- Automatically detects your system timezone
- Handles daylight saving time transitions

## 🐛 Troubleshooting

### Times Still Show UTC After Switching

**Solution**:
1. Close Settings dialog
2. Re-open Settings
3. Verify "Local System Time" is selected
4. If you clicked No on conversion prompt, manually convert:
   - Python console: `db_manager.convert_utc_to_local_timezone()`

### Conversion Prompt Didn't Appear

**Solution**:
- Prompt only shows when switching FROM UTC TO Local
- If you started with Local, no conversion needed
- Already converted data won't prompt again

### Times Look Like Wrong Timezone

**Possible Causes**:
1. System time/timezone incorrect
   - Fix: Update your computer's timezone settings
2. Converted wrong direction
   - Fix: Switch back to UTC, then to Local and convert
3. Data was already in local time
   - Fix: Switch to UTC mode if data was local

### Export Times Don't Match Display

**Check**:
- Ensure you're using latest export (times reflect current mode)
- Verify timezone mode before export
- Excel may auto-format times - check raw values

## 📝 Notes

- **Default**: New installations use Local System Time
- **Migration**: Automatic column addition for existing databases
- **Backward Compatible**: Old databases work without changes
- **Future-Proof**: Easy to add more timezone options later

## 💡 Pro Tips

**Consistent Display**:
- Pick one mode and stick with it
- Convert data once when switching
- Document which mode you're using for team members

**Best Practices**:
- Use Local for personal monitoring
- Use UTC for shared/team databases
- Convert data immediately after switching modes

**Data Analysis**:
- Export with Local mode for readability
- Export with UTC mode for scientific analysis
- Include timezone in export filename

---

**Questions?** Check `festool_monitor.log` or README.md for more details.