# Testing Notifications Guide

Complete guide to set up and test all notification methods.

## 🔧 What Was Fixed

**Problem**: API keys and configs weren't being saved to the database.

**Solution**: 
- Updated settings save to store all configuration data as JSON
- Updated settings load to retrieve and display saved configs
- Added "Test" buttons for each notification type
- Fixed notification sending to use stored configurations

## 🎯 Quick Test - Pushbullet (Easiest!)

### 1. Get Pushbullet API Key

1. Go to [pushbullet.com](https://www.pushbullet.com/)
2. Sign up or log in
3. Click your account icon → **Settings**
4. Scroll to **Access Tokens**
5. Click **Create Access Token**
6. **Copy the token** (starts with `o.`)

### 2. Configure in App

1. Open Festool Recon Monitor
2. Click **Settings** button
3. Go to **Notifications** tab
4. Scroll to **Pushbullet** section
5. Check **"Enable Pushbullet"**
6. Paste your API key in **API Key** field
7. Click **"Test Pushbullet"** button
8. You should see "Test notification sent!"
9. **Check your devices** - you should get a push notification!
10. Click **Save**

### 3. Verify Settings Persist

1. Close Settings dialog
2. Reopen Settings
3. Go to Notifications tab
4. **Your API key should still be there!** ✅

## 📧 Email Notifications (Gmail Example)

### Get Gmail App Password

**Important**: You CANNOT use your regular Gmail password. You need an App Password.

1. Go to [myaccount.google.com/security](https://myaccount.google.com/security)
2. Enable **2-Step Verification** (required for app passwords)
3. Search for "App passwords" or go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
4. Select app: **Mail**
5. Select device: **Other** (type "Festool Monitor")
6. Click **Generate**
7. **Copy the 16-character password** (no spaces)

### Configure Email

1. **Settings** → **Notifications** tab
2. **Email Notifications** section:
   - ✅ Enable Email
   - **SMTP Server**: `smtp.gmail.com`
   - **Port**: `587`
   - **Username**: your full Gmail address
   - **Password**: The 16-char app password (NOT your regular password!)
   - **To Address**: Email where you want notifications
3. Click **"Test Email"** button
4. Check your inbox!
5. Click **Save**

### Other Email Providers

**Outlook/Hotmail:**
- SMTP: `smtp-mail.outlook.com`
- Port: `587`

**Yahoo:**
- SMTP: `smtp.mail.yahoo.com`
- Port: `587`
- Requires app password

**Custom SMTP:**
- Ask your email provider for SMTP settings

## 🔗 Webhook Notifications

### Discord Webhook

1. Open Discord server
2. **Server Settings** → **Integrations**
3. Click **Webhooks** → **New Webhook**
4. Name it (e.g., "Festool Monitor")
5. Select channel
6. **Copy Webhook URL**
7. In app **Settings** → **Notifications**:
   - ✅ Enable Webhook
   - **URL**: Paste Discord webhook URL
   - **Method**: POST
8. Click **"Test Webhook"**
9. Check Discord channel!
10. Click **Save**

### Slack Webhook

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Create new app
3. Enable **Incoming Webhooks**
4. Add webhook to workspace
5. Copy webhook URL
6. Configure same as Discord above

### Custom Webhook

Works with any service that accepts JSON POST requests:
- Zapier
- IFTTT
- Make (Integromat)
- n8n
- Your own API

**Payload Format:**
```json
{
  "product": {
    "name": "Product Name",
    "item_number": "123456",
    "sale_price": 267.12,
    "regular_price": 333.90,
    "posted_at": "2025-10-11T20:00:00"
  },
  "previous_products": [...],
  "timestamp": "2025-10-11T20:00:00"
}
```

## 📱 SMS Notifications (Twilio)

### Setup Twilio

1. Sign up at [twilio.com](https://www.twilio.com/try-twilio)
2. Get a phone number (trial gives you one free)
3. Get credentials from dashboard:
   - **Account SID** (this is your API Key)
   - **Auth Token** (this is your API Secret)
4. For trial: Add your phone to verified numbers

### Configure SMS

1. **Settings** → **Notifications**
2. **SMS Notifications** section:
   - ✅ Enable SMS
   - **Provider**: Twilio
   - **API Key**: Your Account SID
   - **API Secret**: Your Auth Token
   - **From Number**: Your Twilio number (e.g., +12025551234)
   - **To Number**: Your phone (e.g., +12065551234)
3. Click **Save**

**Note**: SMS testing through UI not added yet (would cost money each test). Test by waiting for a real product change.

## ✅ Verification Checklist

After configuring, verify everything works:

### Settings Persistence Test
- [ ] Configure Pushbullet
- [ ] Click Save
- [ ] Close Settings
- [ ] Reopen Settings
- [ ] API key still visible? ✅

### Test Notification Buttons
- [ ] Test Pushbullet → Get notification? ✅
- [ ] Test Email → Email received? ✅
- [ ] Test Webhook → Webhook endpoint hit? ✅

### Real Product Change Test
1. Start monitoring
2. Wait for product change (or simulate by manually changing database)
3. Should receive ALL enabled notifications
4. Check **Statistics** tab → Total Notifications count increases

## 🐛 Troubleshooting

### "Test notification sent" but nothing received

**Pushbullet:**
- Check API key is correct (starts with `o.`)
- Check Pushbullet app is installed on your device
- Check notifications aren't disabled in Pushbullet app

**Email:**
- For Gmail: MUST use App Password, not regular password
- Check spam/junk folder
- Verify SMTP settings are correct
- Check firewall isn't blocking port 587

**Webhook:**
- Check URL is correct and accessible
- Check webhook endpoint is active
- Look at webhook provider's logs

### API Key disappears after saving

**Fixed!** This should no longer happen. If it does:
1. Check `festool_monitor.log` for errors
2. Make sure you clicked **Save** button
3. Try restarting the application

### No notifications on product change

1. **Check Settings** → Notifications → At least one type enabled?
2. **Check monitoring is running** → Should say "Status: Monitoring"
3. **Check logs** → `festool_monitor.log` shows notification attempts
4. **Check watchlist** → If you have keywords, product must match
5. **Test manually** → Use Test buttons to verify config works

### "Module not found" errors

**For Pushbullet:**
```bash
pip install pushbullet.py
```

**For SMS:**
```bash
pip install twilio
```

## 📊 Viewing Notification History

1. Go to **Statistics** tab
2. See "Total Notifications" count
3. Check logs for details:
```bash
# Windows
type festool_monitor.log | findstr "notification"

# Mac/Linux
grep "notification" festool_monitor.log
```

## 🎯 Recommended Setup

**For Most Users:**
1. **Pushbullet** - Easiest, works on all devices
2. **Email** - Reliable backup method
3. **In-App** - Always on by default

**For Discord Users:**
- Use Webhook to post in your server channel
- Great for sharing with friends/family

**For Power Users:**
- All methods enabled
- Use filters to reduce noise
- Include previous products for context

## 💡 Pro Tips

**Reduce Notification Spam:**
- Use **Filters** tab → Add watchlist keywords
- Only get notified for tools you want

**Include Context:**
- **Notifications** tab → Include Previous: 3
- See what was posted before

**Test Before Relying:**
- Always click **Test** buttons after configuration
- Verify notifications work before depending on them

---

## ✨ You're All Set!

Once configured:
1. **Start Monitoring**
2. Notifications will be sent automatically when products change
3. Check notification logs in Statistics tab

**Questions?** Check the main README.md or logs!