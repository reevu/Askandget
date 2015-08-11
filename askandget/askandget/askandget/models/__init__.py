import logging
import re
import hashlib
import datetime
from django.core.urlresolvers import reverse
from django.db.models import signals as django_signals
from django.template import Context
from django.utils.translation import ugettext as _
from django.utils.translation import ungettext
from django.contrib.auth.models import User
from django.utils.safestring import mark_safe
from django.db import models
from django.conf import settings as django_settings
from django.contrib.contenttypes.models import ContentType
from django.core import exceptions as django_exceptions
import askbot
from askbot import exceptions as askbot_exceptions
from askbot import const
from askbot.conf import settings as askbot_settings
from askbot.models.question import Question, QuestionRevision
from askbot.models.question import QuestionView, AnonymousQuestion
from askbot.models.question import FavoriteQuestion
from askbot.models.answer import Answer, AnonymousAnswer, AnswerRevision
from askbot.models.tag import Tag, MarkedTag
from askbot.models.meta import Vote, Comment
from askbot.models.user import EmailFeedSetting, ActivityAuditStatus, Activity
from askbot.models import signals
from askbot.models.badges import award_badges_signal, get_badge, init_badges
#from user import AuthKeyUserAssociation
from askbot.models.repute import BadgeData, Award, Repute
from askbot import auth
from askbot.utils.decorators import auto_now_timestamp
from askbot.utils.slug import slugify
from askbot.utils.diff import textDiff as htmldiff
from askbot.utils.mail import send_mail
from askbot import startup_procedures

startup_procedures.run()

def get_model(model_name):
    return models.get_model('askbot', model_name)

User.add_to_class(
            'status', 
            models.CharField(
                        max_length = 2,
                        default = const.DEFAULT_USER_STATUS,
                        choices = const.USER_STATUS_CHOICES
                    )
        )

User.add_to_class('email_isvalid', models.BooleanField(default=False))
User.add_to_class('email_key', models.CharField(max_length=32, null=True))
#hardcoded initial reputaion of 1, no setting for this one
User.add_to_class('reputation', 
    models.PositiveIntegerField(default=const.MIN_REPUTATION)
)
User.add_to_class('gravatar', models.CharField(max_length=32))
User.add_to_class('gold', models.SmallIntegerField(default=0))
User.add_to_class('silver', models.SmallIntegerField(default=0))
User.add_to_class('bronze', models.SmallIntegerField(default=0))
User.add_to_class(
    'questions_per_page',
    models.SmallIntegerField(
        choices=const.QUESTIONS_PER_PAGE_USER_CHOICES,
        default=10
    )
)
User.add_to_class('last_seen',
                  models.DateTimeField(default=datetime.datetime.now))
User.add_to_class('real_name', models.CharField(max_length=100, blank=True))
User.add_to_class('website', models.URLField(max_length=200, blank=True))
User.add_to_class('location', models.CharField(max_length=100, blank=True))
User.add_to_class('date_of_birth', models.DateField(null=True, blank=True))
User.add_to_class('about', models.TextField(blank=True))
User.add_to_class('hide_ignored_questions', models.BooleanField(default=False))
User.add_to_class('tag_filter_setting',
                    models.CharField(
                                        max_length=16,
                                        choices=const.TAG_EMAIL_FILTER_CHOICES,
                                        default='ignored'
                                     )
                 )
User.add_to_class('new_response_count', models.IntegerField(default=0))
User.add_to_class('seen_response_count', models.IntegerField(default=0))
User.add_to_class('consecutive_days_visit_count', models.IntegerField(default = 0))


def user_get_old_vote_for_post(self, post):
    """returns previous vote for this post
    by the user or None, if does not exist

    raises assertion_error is number of old votes is > 1
    which is illegal
    """
    post_content_type = ContentType.objects.get_for_model(post)
    old_votes = Vote.objects.filter(
                                user = self,
                                content_type = post_content_type,
                                object_id = post.id
                            )
    if len(old_votes) == 0:
        return None
    else:
        assert(len(old_votes) == 1)

    return old_votes[0]


def _assert_user_can(
                        user = None,
                        post = None, #related post (may be parent)
                        admin_or_moderator_required = False,
                        owner_can = False,
                        suspended_owner_cannot = False,
                        owner_min_rep_setting = None,
                        blocked_error_message = None,
                        suspended_error_message = None,
                        min_rep_setting = None,
                        low_rep_error_message = None,
                        owner_low_rep_error_message = None,
                        general_error_message = None
                    ):
    """generic helper assert for use in several
    User.assert_can_XYZ() calls regarding changing content

    user is required and at least one error message

    if assertion fails, method raises exception.PermissionDenied
    with appropriate text as a payload
    """
    if blocked_error_message and user.is_blocked():
        error_message = blocked_error_message
    elif post and owner_can and user == post.get_owner():
        if owner_min_rep_setting:
            if post.get_owner().reputation < owner_min_rep_setting:
                if user.is_moderator() or user.is_administrator():
                    return
                else:
                    assert(owner_low_rep_error_message is not None)
                    raise askbot_exceptions.InsufficientReputation(
                                                owner_low_rep_error_message
                                            )
        if suspended_owner_cannot and user.is_suspended():
            if suspended_error_message:
                error_message = suspended_error_message
            else:
                error_message = general_error_message
            assert(error_message is not None)
            raise django_exceptions.PermissionDenied(error_message)
        else:
            return
        return
    elif suspended_error_message and user.is_suspended():
        error_message = suspended_error_message
    elif user.is_administrator() or user.is_moderator():
        return
    elif low_rep_error_message and user.reputation < min_rep_setting:
        raise askbot_exceptions.InsufficientReputation(low_rep_error_message)
    else:
        if admin_or_moderator_required == False:
            return

    #if admin or moderator is required, then substitute the message
    if admin_or_moderator_required:
        error_message = general_error_message
    assert(error_message is not None)
    raise django_exceptions.PermissionDenied(error_message)

def user_assert_can_unaccept_best_answer(self, answer = None):
    assert(isinstance(answer, Answer))
    if self.is_blocked():
        error_message = _(
                'Sorry, you cannot accept or unaccept best answers '
                'because your account is blocked'
            )
    elif self.is_suspended():
        error_message = _(
                'Sorry, you cannot accept or unaccept best answers '
                'because your account is suspended'
            )
    elif self == answer.question.get_owner():
        if self == answer.get_owner():
            error_message = _(
                'Sorry, you cannot accept or unaccept your own answer '
                'to your own question'
                )
        else:
            return #assertion success
    else:
        error_message = _(
                'Sorry, only original author of the question '
                ' - %(username)s - can accept the best answer'
                ) % {'username': answer.get_owner().username}

    raise django_exceptions.PermissionDenied(error_message)

def user_assert_can_accept_best_answer(self, answer = None):
    assert(isinstance(answer, Answer))
    self.assert_can_unaccept_best_answer(answer)

def user_assert_can_vote_for_post(
                                self, 
                                post = None,
                                direction = None,
                            ):
    """raises exceptions.PermissionDenied exception
    if user can't in fact upvote

    :param:direction can be 'up' or 'down'
    :param:post can be instance of question or answer
    """

    if self == post.author:
        raise django_exceptions.PermissionDenied(_('cannot vote for own posts'))

    blocked_error_message = _(
                'Sorry your account appears to be blocked ' +
                'and you cannot vote - please contact the ' +
                'site administrator to resolve the issue'
            ),
    suspended_error_message = _(
                'Sorry your account appears to be suspended ' +
                'and you cannot vote - please contact the ' +
                'site administrator to resolve the issue'
            )

    assert(direction in ('up', 'down'))

    if direction == 'up':
        min_rep_setting = askbot_settings.MIN_REP_TO_VOTE_UP
        low_rep_error_message = _(
                    ">%(points)s points required to upvote"
                ) % \
                {'points': askbot_settings.MIN_REP_TO_VOTE_UP}
    else:
        min_rep_setting = askbot_settings.MIN_REP_TO_VOTE_DOWN
        low_rep_error_message = _(
                    ">%(points)s points required to downvote"
                ) % \
                {'points': askbot_settings.MIN_REP_TO_VOTE_DOWN}

    _assert_user_can(
        user = self,
        blocked_error_message = blocked_error_message,
        suspended_error_message = suspended_error_message,
        min_rep_setting = min_rep_setting,
        low_rep_error_message = low_rep_error_message
    )


def user_assert_can_upload_file(request_user):

    blocked_error_message = _('Sorry, blocked users cannot upload files')
    suspended_error_message = _('Sorry, suspended users cannot upload files')
    low_rep_error_message = _(
                        'uploading images is limited to users '
                        'with >%(min_rep)s reputation points'
                    ) % {'min_rep': askbot_settings.MIN_REP_TO_UPLOAD_FILES }

    _assert_user_can(
        user = request_user,
        suspended_error_message = suspended_error_message,
        min_rep_setting = askbot_settings.MIN_REP_TO_UPLOAD_FILES,
        low_rep_error_message = low_rep_error_message
    )


def user_assert_can_post_question(self):
    """raises exceptions.PermissionDenied with
    text that has the reason for the denial
    """

    _assert_user_can(
            user = self,
            blocked_error_message = _('blocked users cannot post'),
            suspended_error_message = _('suspended users cannot post'),
    )


def user_assert_can_post_answer(self):
    """same as user_can_post_question
    """
    self.assert_can_post_question()


def user_assert_can_edit_comment(self, comment = None):
    """raises exceptions.PermissionDenied if user
    cannot edit comment with the reason given as message

    only owners, moderators or admins can edit comments
    """
    if self.is_administrator() or self.is_moderator():
        return
    else:
        if comment.user == self:
            now = datetime.datetime.now()
            if now - comment.added_at > datetime.timedelta(0, 600):
                if comment.is_last():
                    return
                error_message = _(
                    'Sorry, comments (except the last one) are editable only within 10 minutes from posting'
                )
                raise django_exceptions.PermissionDenied(error_message)
            return

    error_message = _(
        'Sorry, but only post owners or moderators can edit comments'
    )
    raise django_exceptions.PermissionDenied(error_message)



def user_assert_can_post_comment(self, parent_post = None):
    """raises exceptions.PermissionDenied if
    user cannot post comment

    the reason will be in text of exception
    """

    suspended_error_message = _(
                'Sorry, since your account is suspended '
                'you can comment only your own posts'
            )
    low_rep_error_message = _(
                'Sorry, to comment any post a minimum reputation of '
                '%(min_rep)s points is required. You can still comment '
                'your own posts and answers to your questions'
            ) % {'min_rep': askbot_settings.MIN_REP_TO_LEAVE_COMMENTS}

    try:
        _assert_user_can(
            user = self,
            post = parent_post,
            owner_can = True,
            blocked_error_message = _('blocked users cannot post'),
            suspended_error_message = suspended_error_message,
            min_rep_setting = askbot_settings.MIN_REP_TO_LEAVE_COMMENTS,
            low_rep_error_message = low_rep_error_message,
        )
    except askbot_exceptions.InsufficientReputation, e:
        if isinstance(parent_post, Answer):
            if self == parent_post.question.author:
                return
        raise e

def user_assert_can_see_deleted_post(self, post = None):

    """attn: this assertion is independently coded in
    Question.get_answers call
    """

    error_message = _(
                        'This post has been deleted and can be seen only '
                        'by post owners, site administrators and moderators'
                    )
    _assert_user_can(
        user = self,
        post = post,
        admin_or_moderator_required = True,
        owner_can = True,
        general_error_message = error_message
    )

def user_assert_can_edit_deleted_post(self, post = None):
    assert(post.deleted == True)
    try:
        self.assert_can_see_deleted_post(post)
    except django_exceptions.PermissionDenied, e:
        error_message = _(
                    'Sorry, only moderators, site administrators '
                    'and post owners can edit deleted posts'
                )
        raise django_exceptions.PermissionDenied(error_message)

def user_assert_can_edit_post(self, post = None):
    """assertion that raises exceptions.PermissionDenied
    when user is not authorised to edit this post
    """

    if post.deleted == True:
        self.assert_can_edit_deleted_post(post)
        return

    blocked_error_message = _(
                'Sorry, since your account is blocked '
                'you cannot edit posts'
            )
    suspended_error_message = _(
                'Sorry, since your account is suspended '
                'you can edit only your own posts'
            )
    if post.wiki == True:
        low_rep_error_message = _(
                    'Sorry, to edit wiki posts, a minimum '
                    'reputation of %(min_rep)s is required'
                ) % \
                {'min_rep': askbot_settings.MIN_REP_TO_EDIT_WIKI}
        min_rep_setting = askbot_settings.MIN_REP_TO_EDIT_WIKI
    else:
        low_rep_error_message = _(
                    'Sorry, to edit other people\'s posts, a minimum '
                    'reputation of %(min_rep)s is required'
                ) % \
                {'min_rep': askbot_settings.MIN_REP_TO_EDIT_OTHERS_POSTS}
        min_rep_setting = askbot_settings.MIN_REP_TO_EDIT_OTHERS_POSTS

    _assert_user_can(
        user = self,
        post = post,
        owner_can = True,
        blocked_error_message = blocked_error_message,
        suspended_error_message = suspended_error_message,
        low_rep_error_message = low_rep_error_message,
        min_rep_setting = min_rep_setting
    )


def user_assert_can_edit_question(self, question = None):
    assert(isinstance(question, Question))
    self.assert_can_edit_post(question)


def user_assert_can_edit_answer(self, answer = None):
    assert(isinstance(answer, Answer))
    self.assert_can_edit_post(answer)


def user_assert_can_delete_post(self, post = None):
    if isinstance(post, Question):
        self.assert_can_delete_question(question = post)
    elif isinstance(post, Answer):
        self.assert_can_delete_answer(answer = post)
    elif isinstance(post, Comment):
        self.assert_can_delete_comment(comment = post)

def user_assert_can_restore_post(self, post = None):
    """can_restore_rule is the same as can_delete
    """
    self.assert_can_delete_post(post = post)

def user_assert_can_delete_question(self, question = None):
    """rules are the same as to delete answer,
    except if question has answers already, when owner
    cannot delete unless s/he is and adinistrator or moderator
    """

    #cheating here. can_delete_answer wants argument named
    #"question", so the argument name is skipped
    self.assert_can_delete_answer(question)
    if self == question.get_owner():
        #if there are answers by other people,
        #then deny, unless user in admin or moderator
        answer_count = question.answers.exclude(
                                            author = self,
                                        ).exclude(
                                            score__lte = 0
                                        ).count()

        if answer_count > 0:
            if self.is_administrator() or self.is_moderator():
                return
            else:
                msg = ungettext(
                    'Sorry, cannot delete your question since it '
                    'has an upvoted answer posted by someone else',
                    'Sorry, cannot delete your question since it '
                    'has some upvoted answers posted by other users',
                    answer_count
                )
                raise django_exceptions.PermissionDenied(msg)


def user_assert_can_delete_answer(self, answer = None):
    """intentionally use "post" word in the messages
    instead of "answer", because this logic also applies to 
    assert on deleting question (in addition to some special rules)
    """
    blocked_error_message = _(
                'Sorry, since your account is blocked '
                'you cannot delete posts'
            )
    suspended_error_message = _(
                'Sorry, since your account is suspended '
                'you can delete only your own posts'
            )
    low_rep_error_message = _(
                'Sorry, to deleted other people\' posts, a minimum '
                'reputation of %(min_rep)s is required'
            ) % \
            {'min_rep': askbot_settings.MIN_REP_TO_DELETE_OTHERS_POSTS}
    min_rep_setting = askbot_settings.MIN_REP_TO_DELETE_OTHERS_POSTS

    _assert_user_can(
        user = self,
        post = answer,
        owner_can = True,
        blocked_error_message = blocked_error_message,
        suspended_error_message = suspended_error_message,
        low_rep_error_message = low_rep_error_message,
        min_rep_setting = min_rep_setting
    )


def user_assert_can_close_question(self, question = None):
    assert(isinstance(question, Question) == True)
    blocked_error_message = _(
                'Sorry, since your account is blocked '
                'you cannot close questions'
            )
    suspended_error_message = _(
                'Sorry, since your account is suspended '
                'you cannot close questions'
            )
    low_rep_error_message = _(
                'Sorry, to close other people\' posts, a minimum '
                'reputation of %(min_rep)s is required'
            ) % \
            {'min_rep': askbot_settings.MIN_REP_TO_CLOSE_OTHERS_QUESTIONS}
    min_rep_setting = askbot_settings.MIN_REP_TO_CLOSE_OTHERS_QUESTIONS

    owner_min_rep_setting =  askbot_settings.MIN_REP_TO_CLOSE_OWN_QUESTIONS

    owner_low_rep_error_message = _(
                        'Sorry, to close own question '
                        'a minimum reputation of %(min_rep)s is required'
                    ) % {'min_rep': owner_min_rep_setting}

    _assert_user_can(
        user = self,
        post = question,
        owner_can = True,
        suspended_owner_cannot = True,
        owner_min_rep_setting = owner_min_rep_setting,
        blocked_error_message = blocked_error_message,
        suspended_error_message = suspended_error_message,
        low_rep_error_message = low_rep_error_message,
        owner_low_rep_error_message = owner_low_rep_error_message,
        min_rep_setting = min_rep_setting
    )


def user_assert_can_reopen_question(self, question = None):
    assert(isinstance(question, Question) == True)

    owner_min_rep_setting =  askbot_settings.MIN_REP_TO_REOPEN_OWN_QUESTIONS

    general_error_message = _(
                        'Sorry, only administrators, moderators '
                        'or post owners with reputation > %(min_rep)s '
                        'can reopen questions.'
                    ) % {'min_rep': owner_min_rep_setting }

    owner_low_rep_error_message = _(
                        'Sorry, to reopen own question '
                        'a minimum reputation of %(min_rep)s is required'
                    ) % {'min_rep': owner_min_rep_setting}

    _assert_user_can(
        user = self,
        post = question,
        admin_or_moderator_required = True,
        owner_can = True,
        suspended_owner_cannot = True,
        owner_min_rep_setting = owner_min_rep_setting,
        owner_low_rep_error_message = owner_low_rep_error_message,
        general_error_message = general_error_message
    )


def user_assert_can_flag_offensive(self, post = None):

    assert(post is not None)

    double_flagging_error_message = _('cannot flag message as offensive twice')

    if self.get_flags_for_post(post).count() > 0:
        raise askbot_exceptions.DuplicateCommand(double_flagging_error_message)

    blocked_error_message = _('blocked users cannot flag posts')

    suspended_error_message = _('suspended users cannot flag posts')

    low_rep_error_message = _('need > %(min_rep)s points to flag spam') % \
                        {'min_rep': askbot_settings.MIN_REP_TO_FLAG_OFFENSIVE}
    min_rep_setting = askbot_settings.MIN_REP_TO_FLAG_OFFENSIVE

    _assert_user_can(
        user = self,
        post = post,
        blocked_error_message = blocked_error_message,
        suspended_error_message = suspended_error_message,
        low_rep_error_message = low_rep_error_message,
        min_rep_setting = min_rep_setting
    )
    #one extra assertion
    if self.is_administrator() or self.is_moderator():
        return
    else:
        flag_count_today = self.get_flag_count_posted_today()
        if flag_count_today >= askbot_settings.MAX_FLAGS_PER_USER_PER_DAY:
            flags_exceeded_error_message = _(
                                '%(max_flags_per_day)s exceeded'
                            ) % {
                                    'max_flags_per_day': \
                                    askbot_settings.MAX_FLAGS_PER_USER_PER_DAY
                                }
            raise django_exceptions.PermissionDenied(flags_exceeded_error_message)


def user_assert_can_retag_question(self, question = None):

    if question.deleted == True:
        try:
            self.assert_can_edit_deleted_post(question)
        except django_exceptions.PermissionDenied:
            error_message = _(
                            'Sorry, only question owners, '
                            'site administrators and moderators '
                            'can retag deleted questions'
                        )
            raise django_exceptions.PermissionDenied(error_message)

    blocked_error_message = _(
                'Sorry, since your account is blocked '
                'you cannot retag questions'
            )
    suspended_error_message = _(
                'Sorry, since your account is suspended '
                'you can retag only your own questions'
            )
    low_rep_error_message = _(
                'Sorry, to retag questions a minimum '
                'reputation of %(min_rep)s is required'
            ) % \
            {'min_rep': askbot_settings.MIN_REP_TO_RETAG_OTHERS_QUESTIONS}
    min_rep_setting = askbot_settings.MIN_REP_TO_RETAG_OTHERS_QUESTIONS

    _assert_user_can(
        user = self,
        post = question,
        owner_can = True,
        blocked_error_message = blocked_error_message,
        suspended_error_message = suspended_error_message,
        low_rep_error_message = low_rep_error_message,
        min_rep_setting = min_rep_setting
    )


def user_assert_can_delete_comment(self, comment = None):
    blocked_error_message = _(
                'Sorry, since your account is blocked '
                'you cannot delete comment'
            )
    suspended_error_message = _(
                'Sorry, since your account is suspended '
                'you can delete only your own comments'
            )
    low_rep_error_message = _(
                'Sorry, to delete comments '
                'reputation of %(min_rep)s is required'
            ) % \
            {'min_rep': askbot_settings.MIN_REP_TO_DELETE_OTHERS_COMMENTS}
    min_rep_setting = askbot_settings.MIN_REP_TO_DELETE_OTHERS_COMMENTS

    _assert_user_can(
        user = self,
        post = comment,
        owner_can = True,
        blocked_error_message = blocked_error_message,
        suspended_error_message = suspended_error_message,
        low_rep_error_message = low_rep_error_message,
        min_rep_setting = min_rep_setting
    )


def user_assert_can_revoke_old_vote(self, vote):
    """raises exceptions.PermissionDenied if old vote 
    cannot be revoked due to age of the vote
    """
    if (datetime.datetime.now().day - vote.voted_at.day) \
        >= askbot_settings.MAX_DAYS_TO_CANCEL_VOTE:
        raise django_exceptions.PermissionDenied(_('cannot revoke old vote'))

def user_get_unused_votes_today(self):
    """returns number of votes that are
    still available to the user today
    """
    today = datetime.date.today()
    one_day_interval = (today, today + datetime.timedelta(1))

    used_votes = Vote.objects.filter(
                                user = self, 
                                voted_at__range = one_day_interval
                            ).count()

    available_votes = askbot_settings.MAX_VOTES_PER_USER_PER_DAY - used_votes
    return max(0, available_votes)

def user_post_comment(
                    self,
                    parent_post = None,
                    body_text = None,
                    timestamp = None,
                ):
    """post a comment on behalf of the user
    to parent_post
    """

    if body_text is None:
        raise ValueError('body_text is required to post comment')
    if parent_post is None:
        raise ValueError('parent_post is required to post comment')
    if timestamp is None:
        timestamp = datetime.datetime.now()

    self.assert_can_post_comment(parent_post = parent_post)

    comment = parent_post.add_comment(
                    user = self,
                    comment = body_text,
                    added_at = timestamp,
                )
    award_badges_signal.send(None,
        event = 'post_comment',
        actor = self,
        context_object = comment,
        timestamp = timestamp
    )
    return comment

@auto_now_timestamp
def user_retag_question(
                    self,
                    question = None,
                    tags = None,
                    timestamp = None,
                    silent = False
                ):
    self.assert_can_retag_question(question)
    question.retag(
        retagged_by = self,
        retagged_at = timestamp,
        tagnames = tags,
        silent = silent
    )
    award_badges_signal.send(None,
        event = 'retag_question',
        actor = self,
        context_object = question,
        timestamp = timestamp
    )

@auto_now_timestamp
def user_accept_best_answer(self, answer = None,
                            timestamp = None, cancel = False):
    if cancel:
        return self.unaccept_best_answer(answer = answer, timestamp = timestamp)
    self.assert_can_accept_best_answer(answer)
    if answer.accepted == True:
        return

    prev_accepted_answers = answer.question.answers.filter(accepted = True)
    for prev_answer in prev_accepted_answers:
        auth.onAnswerAcceptCanceled(prev_answer, self)

    auth.onAnswerAccept(answer, self, timestamp = timestamp)
    award_badges_signal.send(None,
        event = 'accept_best_answer',
        actor = self,
        context_object = answer,
        timestamp = timestamp
    )

@auto_now_timestamp
def user_unaccept_best_answer(self, answer = None, timestamp = None):
    self.assert_can_unaccept_best_answer(answer)
    if answer.accepted == False:
        return
    auth.onAnswerAcceptCanceled(answer, self)

@auto_now_timestamp
def user_delete_comment(
                    self,
                    comment = None,
                    timestamp = None
                ):
    self.assert_can_delete_comment(comment = comment)
    comment.delete()

@auto_now_timestamp
def user_delete_answer(
                    self,
                    answer = None,
                    timestamp = None
                ):
    self.assert_can_delete_answer(answer = answer)
    answer.deleted = True
    answer.deleted_by = self 
    answer.deleted_at = timestamp
    answer.save()

    answer.question.update_answer_count()
    logging.debug('updated answer count to %d' % answer.question.answer_count)

    signals.delete_question_or_answer.send(
        sender = answer.__class__,
        instance = answer,
        delete_by = self
    )
    award_badges_signal.send(None,
                event = 'delete_post',
                actor = self,
                context_object = answer,
                timestamp = timestamp
            )


@auto_now_timestamp
def user_delete_question(
                    self,
                    question = None,
                    timestamp = None
                ):
    self.assert_can_delete_question(question = question)

    question.deleted = True
    question.deleted_by = self 
    question.deleted_at = timestamp
    question.save()

    for tag in list(question.tags.all()):
        if tag.used_count == 1:
            tag.deleted = True
            tag.deleted_by = self 
            tag.deleted_at = timestamp
        else:
            tag.used_count = tag.used_count - 1 
        tag.save()

    signals.delete_question_or_answer.send(
        sender = question.__class__,
        instance = question,
        delete_by = self
    )
    award_badges_signal.send(None,
                event = 'delete_post',
                actor = self,
                context_object = question,
                timestamp = timestamp
            )


@auto_now_timestamp
def user_close_question(
                    self,
                    question = None,
                    reason = None,
                    timestamp = None
                ):
    self.assert_can_close_question(question)
    question.closed = True
    question.closed_by = self
    question.closed_at = timestamp
    question.close_reason = reason
    question.save()

@auto_now_timestamp
def user_reopen_question(
                    self,
                    question = None,
                    timestamp = None
                ):
    self.assert_can_reopen_question(question)
    question.closed = False
    question.closed_by = self
    question.closed_at = timestamp
    question.close_reason = None
    question.save()

def user_delete_post(
                    self,
                    post = None,
                    timestamp = None
                ):
    """generic delete method for all kinds of posts

    if there is no use cases for it, the method will be removed
    """
    if isinstance(post, Comment):
        self.delete_comment(comment = post, timestamp = timestamp)
    elif isinstance(post, Answer):
        self.delete_answer(answer = post, timestamp = timestamp)
    elif isinstance(post, Question):
        self.delete_question(question = post, timestamp = timestamp)
    else:
        raise TypeError('either Comment, Question or Answer expected')

def user_restore_post(
                    self,
                    post = None,
                    timestamp = None
                ):
    #here timestamp is not used, I guess added for consistency
    self.assert_can_restore_post(post)
    if isinstance(post, Question) or isinstance(post, Answer):
        post.deleted = False
        post.deleted_by = None 
        post.deleted_at = None 
        post.save()
        if isinstance(post, Answer):
            post.question.update_answer_count()
        elif isinstance(post, Question):
            #todo: make sure that these tags actually exist
            #some may have since been deleted for good 
            #or merged into others
            for tag in list(post.tags.all()):
                if tag.used_count == 1 and tag.deleted:
                    tag.deleted = False
                    tag.deleted_by = None
                    tag.deleted_at = None 
                    tag.save()
    else:
        raise NotImplementedError()

def user_post_question(
                    self,
                    title = None,
                    body_text = None,
                    tags = None,
                    wiki = False,
                    timestamp = None
                ):

    self.assert_can_post_question()

    if title is None:
        raise ValueError('Title is required to post question')
    if  body_text is None:
        raise ValueError('Text body is required to post question')
    if tags is None:
        raise ValueError('Tags are required to post question')
    if timestamp is None:
        timestamp = datetime.datetime.now()

    question = Question.objects.create_new(
                                    author = self,
                                    title = title,
                                    text = body_text,
                                    tagnames = tags,
                                    added_at = timestamp,
                                    wiki = wiki
                                )
    return question

def user_edit_comment(self, comment = None, body_text = None):
    """apply edit to a comment, the method does not
    change the comments timestamp and no signals are sent
    """
    self.assert_can_edit_comment(comment)
    comment.comment = body_text
    comment.parse_and_save(author = self)

@auto_now_timestamp
def user_edit_question(
                    self,
                    question = None,
                    title = None,
                    body_text = None,
                    revision_comment = None,
                    tags = None,
                    wiki = False,
                    timestamp = None
                ):
    self.assert_can_edit_question(question)
    question.apply_edit(
        edited_at = timestamp,
        edited_by = self,
        title = title,
        text = body_text,
        #todo: summary name clash in question and question revision
        comment = revision_comment,
        tags = tags,
        wiki = wiki,
    )
    award_badges_signal.send(None,
        event = 'edit_question',
        actor = self,
        context_object = question,
        timestamp = timestamp
    )

@auto_now_timestamp
def user_edit_answer(
                    self,
                    answer = None,
                    body_text = None,
                    revision_comment = None,
                    wiki = False,
                    timestamp = None
                ):
    self.assert_can_edit_answer(answer)
    answer.apply_edit(
        edited_at = timestamp,
        edited_by = self,
        text = body_text,
        comment = revision_comment,
        wiki = wiki,
    )
    award_badges_signal.send(None,
        event = 'edit_answer',
        actor = self,
        context_object = answer,
        timestamp = timestamp
    )

def user_is_following(self, followed_item):
    if isinstance(followed_item, Question):
        followers = User.objects.filter(
                                id = self.id,
                                followed_questions = followed_item,
                            )
        if self in followers:
            return True
        else:
            return False
    else:
        raise NotImplementedError('function only works for questions so far')

def user_post_answer(
                    self,
                    question = None,
                    body_text = None,
                    follow = False,
                    wiki = False,
                    timestamp = None
                ):

    self.assert_can_post_answer()

    if not isinstance(question, Question):
        raise TypeError('question argument must be provided')
    if body_text is None:
        raise ValueError('Body text is required to post answer')
    if timestamp is None:
        timestamp = datetime.datetime.now()
    answer = Answer.objects.create_new(
                                    question = question,
                                    author = self,
                                    text = body_text,
                                    added_at = timestamp,
                                    email_notify = follow,
                                    wiki = wiki
                                )
    award_badges_signal.send(None,
        event = 'post_answer',
        actor = self,
        context_object = answer
    )
    return answer

def user_visit_question(self, question = None, timestamp = None):
    """create a QuestionView record
    on behalf of the user represented by the self object
    and mark it as taking place at timestamp time

    and remove pending on-screen notifications about anything in 
    the post - question, answer or comments
    """
    if not isinstance(question, Question):
        raise TypeError('question type expected, have %s' % type(question))
    if timestamp is None:
        timestamp = datetime.datetime.now()

    try:
        question_view = QuestionView.objects.get(
                                        who = self,
                                        question = question
                                    )
    except QuestionView.DoesNotExist:
        question_view = QuestionView(
                                who = self, 
                                question = question
                            )
    question_view.when = timestamp
    question_view.save()

    #filter memo objects on response activities directed to the qurrent user
    #that refer to the children of the currently
    #viewed question and clear them for the current user
    ACTIVITY_TYPES = const.RESPONSE_ACTIVITY_TYPES_FOR_DISPLAY
    ACTIVITY_TYPES += (const.TYPE_ACTIVITY_MENTION,)

    audit_records = ActivityAuditStatus.objects.filter(
                        user = self,
                        status = ActivityAuditStatus.STATUS_NEW,
                        activity__question = question
                    )

    cleared_record_count = audit_records.filter(
                                activity__activity_type__in = ACTIVITY_TYPES
                            ).update(
                                status=ActivityAuditStatus.STATUS_SEEN
                            )
    if cleared_record_count > 0:
        self.decrement_response_count(cleared_record_count)
        self.save()

    #finally, mark admin memo objects if applicable
    #the admin response counts are not denormalized b/c they are easy to obtain
    if self.is_moderator() or self.is_administrator():
        audit_records.filter(
                activity__activity_type = const.TYPE_ACTIVITY_MARK_OFFENSIVE
        ).update(
            status=ActivityAuditStatus.STATUS_SEEN
        )


def user_is_username_taken(cls,username):
    try:
        cls.objects.get(username=username)
        return True
    except cls.MultipleObjectsReturned:
        return True
    except cls.DoesNotExist:
        return False

def user_is_administrator(self):
    """checks whether user in the forum site administrator
    the admin must be both superuser and staff member
    the latter is because staff membership is required
    to access the live settings"""
    return (self.is_superuser and self.is_staff)

def user_remove_admin_status(self):
    self.is_staff = False
    self.is_superuser = False

def user_set_admin_status(self):
    self.is_staff = True
    self.is_superuser = True
    
def user_is_moderator(self):
    return (self.status == 'm' and self.is_administrator() == False)

def user_is_suspended(self):
    return (self.status == 's')

def user_is_blocked(self):
    return (self.status == 'b')

def user_is_watched(self):
    return (self.status == 'w')

def user_is_approved(self):
    return (self.status == 'a')

def user_set_status(self, new_status):
    """sets new status to user

    this method understands that administrator status is
    stored in the User.is_superuser field, but
    everything else in User.status field

    there is a slight aberration - administrator status
    can be removed, but not added yet

    if new status is applied to user, then the record is 
    committed to the database
    """
    #m - moderator
    #s - suspended
    #b - blocked
    #w - watched
    #a - approved (regular user)
    assert(new_status in ('m', 's', 'b', 'w', 'a'))
    if new_status == self.status:
        return

    #clear admin status if user was an administrator
    #because this function is not dealing with the site admins
    if self.is_administrator():
        self.remove_admin_status()

    self.status = new_status
    self.save()

@auto_now_timestamp
def user_moderate_user_reputation(
                                self,
                                user = None,
                                reputation_change = 0,
                                comment = None,
                                timestamp = None
                            ):
    """add or subtract reputation of other user
    """
    if reputation_change == 0:
        return
    if comment == None:
        raise ValueError('comment is required to moderate user reputation')

    new_rep = user.reputation + reputation_change
    if new_rep < 1:
        new_rep = 1 #todo: magic number
        reputation_change = 1 - user.reputation

    user.reputation = new_rep
    user.save()

    #any question. This is necessary because reputes are read in the
    #user_reputation view with select_related('question__title') and it fails if 
    #ForeignKey is nullable even though it should work (according to the manual)
    #probably a bug in the Django ORM
    #fake_question = Question.objects.all()[:1][0]
    #so in cases where reputation_type == 10
    #question record is fake and is ignored
    #this bug is hidden in call Repute.get_explanation_snippet()
    repute = Repute(
                        user = user,
                        comment = comment,
                        #question = fake_question,
                        reputed_at = timestamp,
                        reputation_type = 10, #todo: fix magic number
                        reputation = user.reputation
                    )
    if reputation_change < 0:
        repute.negative = -1 * reputation_change
    else:
        repute.positive = reputation_change
    repute.save()

def user_get_status_display(self, soft = False):
    if self.is_administrator():
        return _('Site Adminstrator')
    elif self.is_moderator():
        return _('Forum Moderator')
    elif self.is_suspended():
        return  _('Suspended User')
    elif self.is_blocked():
        return _('Blocked User')
    elif soft == True:
        return _('Registered User')
    elif self.is_watched():
        return _('Watched User')
    elif self.is_approved():
        return _('Approved User')
    else:
        raise ValueError('Unknown user status')


def user_can_moderate_user(self, other):
    if self.is_administrator():
        return True
    elif self.is_moderator():
        if other.is_moderator() or other.is_administrator():
            return False
        else:
            return True
    else:
        return False


def user_get_q_sel_email_feed_frequency(self):
    try:
        feed_setting = EmailFeedSetting.objects.get(
                                        subscriber=self,
                                        feed_type='q_sel'
                                    )
    except Exception, e:
        raise e
    return feed_setting.frequency

def get_messages(self):
    messages = []
    for m in self.message_set.all():
        messages.append(m.message)
    return messages

def delete_messages(self):
    self.message_set.all().delete()

#todo: find where this is used and replace with get_absolute_url
def get_profile_url(self):
    """Returns the URL for this User's profile."""
    return reverse(
                'user_profile', 
                kwargs={'id':self.id, 'slug':slugify(self.username)}
            )

def user_get_absolute_url(self):
    return self.get_profile_url()

def get_profile_link(self):
    profile_link = u'<a href="%s">%s</a>' \
        % (self.get_profile_url(),self.username)

    return mark_safe(profile_link)

def user_get_karma_summary(self):
    """returns human readable sentence about
    status of user's karma"""
    return _("%(username)s karma is %(reputation)s") % \
            {'username': self.username, 'reputation': self.reputation}

def user_get_badge_summary(self):
    """returns human readable sentence about
    number of badges of different levels earned
    by the user. It is assumed that user has some badges"""
    badge_bits = list()
    if self.gold:
        bit = ungettext(
                'one gold badge',
                '%(count)d gold badges',
                self.gold
            ) % {'count': self.gold}
        badge_bits.append(bit)
    if self.silver:
        bit = ungettext(
                'one silver badge',
                '%(count)d silver badges',
                self.gold
            ) % {'count': self.silver}
        badge_bits.append(bit)
    if self.silver:
        bit = ungettext(
                'one bronze badge',
                '%(count)d bronze badges',
                self.gold
            ) % {'count': self.bronze}
        badge_bits.append(bit)

    if len(badge_bits) == 1:
        badge_str = badge_bits[0]
    elif len(badge_bits) > 1:
        last_bit = badge_bits.pop()
        badge_str = ', '.join(badge_bits)
        badge_str = _('%(item1)s and %(item2)s') % \
                    {'item1': badge_str, 'item2': last_bit}
    else:
        raise ValueError('user must have badges to call this function')
    return _("%(user)s has %(badges)s") % {'user': self.username, 'badges':badge_str}

#series of methods for user vote-type commands
#same call signature func(self, post, timestamp=None, cancel=None)
#note that none of these have business logic checks internally
#these functions are used by the askbot app and
#by the data importer jobs from say stackexchange, where internal rules
#may be different
#maybe if we do use business rule checks here - we should add
#some flag allowing to bypass them for things like the data importers
def toggle_favorite_question(self, question, timestamp=None, cancel=False):
    """cancel has no effect here, but is important for the SE loader
    it is hoped that toggle will work and data will be consistent
    but there is no guarantee, maybe it's better to be more strict 
    about processing the "cancel" option
    another strange thing is that this function unlike others below
    returns a value
    """
    try:
        fave = FavoriteQuestion.objects.get(question=question, user=self)
        fave.delete()
        result = False
        question.update_favorite_count()
    except FavoriteQuestion.DoesNotExist:
        if timestamp is None:
            timestamp = datetime.datetime.now()
        fave = FavoriteQuestion(
            question = question,
            user = self,
            added_at = timestamp,
        )
        fave.save()
        result = True
        question.update_favorite_count()
        award_badges_signal.send(None,
            event = 'select_favorite_question',
            actor = self,
            context_object = question,
            timestamp = timestamp
        )
    return result

VOTES_TO_EVENTS = {
    (Vote.VOTE_UP, 'answer'): 'upvote_answer',
    (Vote.VOTE_UP, 'question'): 'upvote_question',
    (Vote.VOTE_DOWN, 'question'): 'downvote',
    (Vote.VOTE_DOWN, 'answer'): 'downvote'
}
@auto_now_timestamp
def _process_vote(user, post, timestamp=None, cancel=False, vote_type=None):
    """"private" wrapper function that applies post upvotes/downvotes
    and cancelations
    """
    post_type = ContentType.objects.get_for_model(post)
    #get or create the vote object
    #return with noop in some situations
    try:
        vote = Vote.objects.get(
                    user = user,
                    content_type = post_type,
                    object_id = post.id,
                )
    except Vote.DoesNotExist:
        vote = None
    if cancel:
        if vote == None:
            return
        elif vote.is_opposite(vote_type):
            return
        else:
            #we would call vote.delete() here
            #but for now all that is handled by the
            #legacy askbot.auth functions
            #vote.delete()
            pass
    else:
        if vote == None:
            vote = Vote(
                    user = user,
                    content_object = post,
                    vote = vote_type,
                    voted_at = timestamp,
                    )
        elif vote.is_opposite(vote_type):
            vote.vote = vote_type
        else:
            return

    #do the actual work
    if vote_type == Vote.VOTE_UP:
        if cancel:
            auth.onUpVotedCanceled(vote, post, user, timestamp)
            return None
        else:
            auth.onUpVoted(vote, post, user, timestamp)
    elif vote_type == Vote.VOTE_DOWN:
        if cancel:
            auth.onDownVotedCanceled(vote, post, user, timestamp)
            return None
        else:
            auth.onDownVoted(vote, post, user, timestamp)

    event = VOTES_TO_EVENTS.get((vote_type, post.post_type), None)
    if event:
        award_badges_signal.send(None,
                    event = event,
                    actor = user,
                    context_object = post,
                    timestamp = timestamp
                )
    return vote

def user_unfollow_question(self, question = None):
    if self in question.followed_by.all():
        question.followed_by.remove(self)

def user_follow_question(self, question = None):
    if self not in question.followed_by.all():
        question.followed_by.add(self)

def upvote(self, post, timestamp=None, cancel=False):
    return _process_vote(
        self,post,
        timestamp=timestamp,
        cancel=cancel,
        vote_type=Vote.VOTE_UP
    )

def downvote(self, post, timestamp=None, cancel=False):
    return _process_vote(
        self,post,
        timestamp=timestamp,
        cancel=cancel,
        vote_type=Vote.VOTE_DOWN
    )

@auto_now_timestamp
def flag_post(user, post, timestamp=None, cancel=False):
    if cancel:#todo: can't unflag?
        return

    user.assert_can_flag_offensive(post = post)
    auth.onFlaggedItem(post, user, timestamp=timestamp)
    award_badges_signal.send(None,
        event = 'flag_post',
        actor = user,
        context_object = post,
        timestamp = timestamp
    )

def user_get_flags(self):
    """return flag Activity query set
    for all flags set by te user"""
    return Activity.objects.filter(
                        user = self,
                        activity_type = const.TYPE_ACTIVITY_MARK_OFFENSIVE
                    )

def user_get_flag_count_posted_today(self):
    """return number of flags the user has posted
    within last 24 hours"""
    today = datetime.date.today()
    time_frame = (today, today + datetime.timedelta(1))
    flags = self.get_flags()
    return flags.filter(active_at__range = time_frame).count()

def user_get_flags_for_post(self, post):
    """return query set for flag Activity items
    posted by users for a given post obeject
    """
    post_content_type = ContentType.objects.get_for_model(post)
    flags = self.get_flags()
    return flags.filter(content_type = post_content_type, object_id=post.id)

def user_increment_response_count(user):
    """increment response counter for user
    by one
    """
    user.new_response_count += 1

def user_decrement_response_count(user, amount=1):
    """decrement response count for the user 
    by one, log critical error if count would go below zero
    but limit decrementation at zero exactly
    """
    assert(amount > 0)
    user.seen_response_count += amount
    if user.new_response_count >= amount:
        user.new_response_count -= amount
    user.clean_response_counts()

def user_clean_response_counts(user):
    ""
    if user.new_response_count < 0:
        user.new_response_count = 0
        logging.critical(
                'new response count wanted to go below zero for %s' % user.username
            )
    if user.seen_response_count < 0:
        user.seen_response_count = 0
        logging.critical(
                'seen response count wanted to go below zero form %s' % user.username
            )

def user_receive_reputation(self, num_points):
    new_points = self.reputation + num_points
    if new_points > 0:
        self.reputation = new_points
    else:
        self.reputation = const.MIN_REPUTATION

User.add_to_class('is_username_taken',classmethod(user_is_username_taken))
User.add_to_class(
            'get_q_sel_email_feed_frequency',
            user_get_q_sel_email_feed_frequency
        )
User.add_to_class('get_absolute_url', user_get_absolute_url)
User.add_to_class('post_question', user_post_question)
User.add_to_class('edit_question', user_edit_question)
User.add_to_class('retag_question', user_retag_question)
User.add_to_class('post_answer', user_post_answer)
User.add_to_class('edit_answer', user_edit_answer)
User.add_to_class('post_comment', user_post_comment)
User.add_to_class('edit_comment', user_edit_comment)
User.add_to_class('delete_post', user_delete_post)
User.add_to_class('visit_question', user_visit_question)
User.add_to_class('upvote', upvote)
User.add_to_class('downvote', downvote)
User.add_to_class('flag_post', flag_post)
User.add_to_class('receive_reputation', user_receive_reputation)
User.add_to_class('get_flags', user_get_flags)
User.add_to_class('get_flag_count_posted_today', user_get_flag_count_posted_today)
User.add_to_class('get_flags_for_post', user_get_flags_for_post)
User.add_to_class('get_profile_url', get_profile_url)
User.add_to_class('get_profile_link', get_profile_link)
User.add_to_class('get_messages', get_messages)
User.add_to_class('delete_messages', delete_messages)
User.add_to_class('toggle_favorite_question', toggle_favorite_question)
User.add_to_class('follow_question', user_follow_question)
User.add_to_class('unfollow_question', user_unfollow_question)
User.add_to_class('is_following', user_is_following)
User.add_to_class('decrement_response_count', user_decrement_response_count)
User.add_to_class('increment_response_count', user_increment_response_count)
User.add_to_class('clean_response_counts', user_clean_response_counts)
User.add_to_class('is_administrator', user_is_administrator)
User.add_to_class('set_admin_status', user_set_admin_status)
User.add_to_class('remove_admin_status', user_remove_admin_status)
User.add_to_class('is_moderator', user_is_moderator)
User.add_to_class('is_approved', user_is_approved)
User.add_to_class('is_watched', user_is_watched)
User.add_to_class('is_suspended', user_is_suspended)
User.add_to_class('is_blocked', user_is_blocked)
User.add_to_class('can_moderate_user', user_can_moderate_user)
User.add_to_class('moderate_user_reputation', user_moderate_user_reputation)
User.add_to_class('set_status', user_set_status)
User.add_to_class('get_status_display', user_get_status_display)
User.add_to_class('get_old_vote_for_post', user_get_old_vote_for_post)
User.add_to_class('get_unused_votes_today', user_get_unused_votes_today)
User.add_to_class('delete_comment', user_delete_comment)
User.add_to_class('delete_question', user_delete_question)
User.add_to_class('delete_answer', user_delete_answer)
User.add_to_class('restore_post', user_restore_post)
User.add_to_class('close_question', user_close_question)
User.add_to_class('reopen_question', user_reopen_question)
User.add_to_class('accept_best_answer', user_accept_best_answer)
User.add_to_class('unaccept_best_answer', user_unaccept_best_answer)

#assertions
User.add_to_class('assert_can_vote_for_post', user_assert_can_vote_for_post)
User.add_to_class('assert_can_revoke_old_vote', user_assert_can_revoke_old_vote)
User.add_to_class('assert_can_upload_file', user_assert_can_upload_file)
User.add_to_class('assert_can_post_question', user_assert_can_post_question)
User.add_to_class('assert_can_post_answer', user_assert_can_post_answer)
User.add_to_class('assert_can_post_comment', user_assert_can_post_comment)
User.add_to_class('assert_can_edit_post', user_assert_can_edit_post)
User.add_to_class('assert_can_edit_deleted_post', user_assert_can_edit_deleted_post)
User.add_to_class('assert_can_see_deleted_post', user_assert_can_see_deleted_post)
User.add_to_class('assert_can_edit_question', user_assert_can_edit_question)
User.add_to_class('assert_can_edit_answer', user_assert_can_edit_answer)
User.add_to_class('assert_can_close_question', user_assert_can_close_question)
User.add_to_class('assert_can_reopen_question', user_assert_can_reopen_question)
User.add_to_class('assert_can_flag_offensive', user_assert_can_flag_offensive)
User.add_to_class('assert_can_retag_question', user_assert_can_retag_question)
#todo: do we need assert_can_delete_post
User.add_to_class('assert_can_delete_post', user_assert_can_delete_post)
User.add_to_class('assert_can_restore_post', user_assert_can_restore_post)
User.add_to_class('assert_can_delete_comment', user_assert_can_delete_comment)
User.add_to_class('assert_can_edit_comment', user_assert_can_edit_comment)
User.add_to_class('assert_can_delete_answer', user_assert_can_delete_answer)
User.add_to_class('assert_can_delete_question', user_assert_can_delete_question)
User.add_to_class('assert_can_accept_best_answer', user_assert_can_accept_best_answer)
User.add_to_class(
        'assert_can_unaccept_best_answer',
        user_assert_can_unaccept_best_answer
    )

#todo: move this to askbot/utils ??
def format_instant_notification_email(
                                        to_user = None,
                                        from_user = None,
                                        post = None,
                                        update_type = None,
                                        template = None,
                                    ):
    """
    returns text of the instant notification body
    and subject line

    that is built when post is updated
    only update_types in const.RESPONSE_ACTIVITY_TYPE_MAP_FOR_TEMPLATES
    are supported
    """

    site_url = askbot_settings.APP_URL
    origin_post = post.get_origin_post()
    #todo: create a better method to access "sub-urls" in user views
    user_subscriptions_url = site_url + to_user.get_absolute_url() + \
                            '?sort=email_subscriptions'

    if update_type == 'question_comment':
        assert(isinstance(post, Comment))
        assert(isinstance(post.content_object, Question))
        subject_line = _(
                    'Re: "%(title)s"'
                ) % {'title': origin_post.title}
    elif update_type == 'answer_comment':
        assert(isinstance(post, Comment))
        assert(isinstance(post.content_object, Answer))
        subject_line = _(
                    'Re: "%(title)s"'
                ) % {'title': origin_post.title}
    elif update_type == 'answer_update':
        assert(isinstance(post, Answer))
        subject_line = _(
                    'Re: "%(title)s"'
                ) % {'title': origin_post.title}
    elif update_type == 'new_answer':
        assert(isinstance(post, Answer))
        subject_line = _(
                    'Re: "%(title)s"'
                ) % {'title': origin_post.title}
    elif update_type == 'question_update':
        assert(isinstance(post, Question))
        subject_line = _(
                    'Question: "%(title)s"'
                ) % {'title': origin_post.title}
    elif update_type == 'new_question':
        assert(isinstance(post, Question))
        subject_line = _(
                    'Question: "%(title)s"'
                ) % {'title': origin_post.title}
    else:
        raise ValueError('unexpected update_type %s' % update_type)

    if update_type.endswith('update'):
        assert('comment' not in update_type)
        revisions = post.revisions.all()[:2]
        assert(len(revisions) == 2)
        content_preview = htmldiff(
                            revisions[1].as_html(),
                            revisions[0].as_html(),
                            ins_start = '<b><u style="background-color:#cfc">',
                            ins_end = '</u></b>',
                            del_start = '<del style="color:#600;background-color:#fcc">',
                            del_end = '</del>'
                        )
        #todo: remove hardcoded style
    else:
        content_preview = post.html
        tag_style = "white-space: nowrap; " \
                    + "font-size: 11px; color: #333;" \
                    + "background-color: #EEE;" \
                    + "border-left: 3px solid #777;" \
                    + "border-top: 1px solid #EEE;" \
                    + "border-bottom: 1px solid #CCC;" \
                    + "border-right: 1px solid #CCC;" \
                    + "padding: 1px 8px 1px 8px;" \
                    + "margin-right:3px;"
        if post.post_type == 'question':#add tags to the question
            content_preview += '<div>'
            for tag_name in post.get_tag_names():
                content_preview += '<span style="%s">%s</span>' % (tag_style, tag_name)
            content_preview += '</div>'

    update_data = {
        'update_author_name': from_user.username,
        'receiving_user_name': to_user.username,
        'content_preview': content_preview,#post.get_snippet()
        'update_type': update_type,
        'post_url': site_url + post.get_absolute_url(),
        'origin_post_title': origin_post.title,
        'user_subscriptions_url': user_subscriptions_url,
    }
    return subject_line, template.render(Context(update_data))

#todo: action
def send_instant_notifications_about_activity_in_post(
                                                update_activity = None,
                                                post = None,
                                                recipients = None,
                                            ):
    """
    function called when posts are updated
    newly mentioned users are carried through to reduce
    database hits
    """

    if recipients is None:
        return

    acceptable_types = const.RESPONSE_ACTIVITY_TYPES_FOR_INSTANT_NOTIFICATIONS

    if update_activity.activity_type not in acceptable_types:
        return

    from askbot.skins.loaders import ENV
    template = ENV.get_template('instant_notification.html')

    update_type_map = const.RESPONSE_ACTIVITY_TYPE_MAP_FOR_TEMPLATES
    update_type = update_type_map[update_activity.activity_type]

    origin_post = post.get_origin_post()
    for user in recipients:

        subject_line, body_text = format_instant_notification_email(
                        to_user = user,
                        from_user = update_activity.user,
                        post = post,
                        update_type = update_type,
                        template = template,
                    )
        #todo: this could be packaged as an "action" - a bundle
        #of executive function with the activity log recording
        send_mail(
            subject_line = subject_line,
            body_text = body_text,
            recipient_list = [user.email],
            related_object = origin_post,
            activity_type = const.TYPE_ACTIVITY_EMAIL_UPDATE_SENT
        )


#todo: move to utils
def calculate_gravatar_hash(instance, **kwargs):
    """Calculates a User's gravatar hash from their email address."""
    if kwargs.get('raw', False):
        return
    clean_email = instance.email.strip().lower()
    instance.gravatar = hashlib.md5(clean_email).hexdigest()


def record_post_update_activity(
        post,
        newly_mentioned_users = list(), 
        updated_by = None,
        timestamp = None,
        created = False,
        **kwargs
    ):
    """called upon signal askbot.models.signals.post_updated
    which is sent at the end of save() method in posts
    """
    assert(timestamp != None)
    assert(updated_by != None)

    #todo: take into account created == True case
    (activity_type, update_object) = post.get_updated_activity_data(created)

    update_activity = Activity(
                    user = updated_by,
                    active_at = timestamp, 
                    content_object = post, 
                    activity_type = activity_type,
                    question = post.get_origin_post()
                )
    update_activity.save()

    #what users are included depends on the post type
    #for example for question - all Q&A contributors
    #are included, for comments only authors of comments and parent 
    #post are included
    recipients = post.get_response_receivers(
                                exclude_list = [updated_by, ]
                            )

    update_activity.add_recipients(recipients)

    assert(updated_by not in recipients)

    for user in set(recipients) | set(newly_mentioned_users):
        user.increment_response_count()
        user.save()

    #todo: weird thing is that only comments need the recipients
    #todo: debug these calls and then uncomment in the repo
    #argument to this call
    notification_subscribers = post.get_instant_notification_subscribers(
                                    potential_subscribers = recipients,
                                    mentioned_users = newly_mentioned_users,
                                    exclude_list = [updated_by, ]
                                )

    send_instant_notifications_about_activity_in_post(
                            update_activity = update_activity,
                            post = post,
                            recipients = notification_subscribers,
                        )


def record_award_event(instance, created, **kwargs):
    """
    After we awarded a badge to user, we need to 
    record this activity and notify user.
    We also recaculate awarded_count of this badge and user information.
    """
    if created:
        #todo: change this to community user who gives the award
        activity = Activity(
                        user=instance.user,
                        active_at=instance.awarded_at,
                        content_object=instance,
                        activity_type=const.TYPE_ACTIVITY_PRIZE
                    )
        activity.save()
        activity.add_recipients([instance.user])

        instance.badge.awarded_count += 1
        instance.badge.save()

        badge = get_badge(instance.badge.slug)

        if badge.level == const.GOLD_BADGE:
            instance.user.gold += 1
        if badge.level == const.SILVER_BADGE:
            instance.user.silver += 1
        if badge.level == const.BRONZE_BADGE:
            instance.user.bronze += 1
        instance.user.save()

def notify_award_message(instance, created, **kwargs):
    """
    Notify users when they have been awarded badges by using Django message.
    """
    if created:
        user = instance.user

        badge = get_badge(instance.badge.slug)

        msg = _(u"Congratulations, you have received a badge '%(badge_name)s'. "
                u"Check out <a href=\"%(user_profile)s\">your profile</a>.") \
                % {
                    'badge_name':badge.name, 
                    'user_profile':user.get_profile_url()
                } 

        user.message_set.create(message=msg)

def record_answer_accepted(instance, created, **kwargs):
    """
    when answer is accepted, we record this for question author 
    - who accepted it.
    """
    if not created and instance.accepted:
        activity = Activity(
                        user=instance.question.author,
                        active_at=datetime.datetime.now(),
                        content_object=instance,
                        activity_type=const.TYPE_ACTIVITY_MARK_ANSWER,
                        question=instance.question
                    )
        activity.save()
        recipients = instance.get_author_list(
                                    exclude_list = [instance.question.author]
                                )
        activity.add_recipients(recipients)

def record_user_visit(user, timestamp, **kwargs):
    """
    when user visits any pages, we update the last_seen and
    consecutive_days_visit_count
    """
    prev_last_seen = user.last_seen
    user.last_seen = timestamp
    if (user.last_seen - prev_last_seen).days == 1:
        user.consecutive_days_visit_count += 1
        award_badges_signal.send(None,
            event = 'site_visit',
            actor = user,
            context_object = user,
            timestamp = timestamp
        )
    user.save()


def record_vote(instance, created, **kwargs):
    """
    when user have voted
    """
    if created:
        if instance.vote == 1:
            vote_type = const.TYPE_ACTIVITY_VOTE_UP
        else:
            vote_type = const.TYPE_ACTIVITY_VOTE_DOWN

        activity = Activity(
                        user=instance.user,
                        active_at=instance.voted_at,
                        content_object=instance,
                        activity_type=vote_type
                    )
        #todo: problem cannot access receiving user here
        activity.save()


def record_cancel_vote(instance, **kwargs):
    """
    when user canceled vote, the vote will be deleted.
    """
    activity = Activity(
                    user=instance.user, 
                    active_at=datetime.datetime.now(), 
                    content_object=instance, 
                    activity_type=const.TYPE_ACTIVITY_CANCEL_VOTE
                )
    #todo: same problem - cannot access receiving user here
    activity.save()


#todo: weird that there is no record delete answer or comment
#is this even necessary to keep track of?
def record_delete_question(instance, delete_by, **kwargs):
    """
    when user deleted the question
    """
    if instance.__class__ == "Question":
        activity_type = const.TYPE_ACTIVITY_DELETE_QUESTION
    else:
        activity_type = const.TYPE_ACTIVITY_DELETE_ANSWER

    activity = Activity(
                    user=delete_by, 
                    active_at=datetime.datetime.now(), 
                    content_object=instance, 
                    activity_type=activity_type,
                    question = instance.get_origin_post()
                )
    #no need to set receiving user here
    activity.save()

def record_flag_offensive(instance, mark_by, **kwargs):
    activity = Activity(
                    user=mark_by, 
                    active_at=datetime.datetime.now(), 
                    content_object=instance, 
                    activity_type=const.TYPE_ACTIVITY_MARK_OFFENSIVE,
                    question=instance.get_origin_post()
                )
    activity.save()
#   todo: report authors that their post is flagged offensive
#    recipients = instance.get_author_list(
#                                        exclude_list = [mark_by]
#                                    )
    recipients = User.objects.filter(
                    models.Q(is_superuser=True) | models.Q(status='m')
                )
    activity.add_recipients(recipients)

def record_update_tags(question, tags, user, timestamp, **kwargs):
    """
    This function sends award badges signal on each updated tag
    the badges that respond to the 'ta
    """
    for tag in tags:
        award_badges_signal.send(None,
            event = 'update_tag',
            actor = user,
            context_object = tag,
            timestamp = timestamp
        )

    activity = Activity(
                    user=user,
                    active_at=datetime.datetime.now(),
                    content_object=question,
                    activity_type=const.TYPE_ACTIVITY_UPDATE_TAGS,
                    question = question
                )
    activity.save()

def record_favorite_question(instance, created, **kwargs):
    """
    when user add the question in him favorite questions list.
    """
    if created:
        activity = Activity(
                        user=instance.user, 
                        active_at=datetime.datetime.now(), 
                        content_object=instance, 
                        activity_type=const.TYPE_ACTIVITY_FAVORITE,
                        question=instance.question
                    )
        activity.save()
        recipients = instance.question.get_author_list(
                                            exclude_list = [instance.user]
                                        )
        activity.add_recipients(recipients)

def record_user_full_updated(instance, **kwargs):
    activity = Activity(
                    user=instance, 
                    active_at=datetime.datetime.now(), 
                    content_object=instance, 
                    activity_type=const.TYPE_ACTIVITY_USER_FULL_UPDATED
                )
    activity.save()

def post_stored_anonymous_content(
                                sender,
                                user,
                                session_key,
                                signal,
                                *args,
                                **kwargs):

    aq_list = AnonymousQuestion.objects.filter(session_key = session_key)
    aa_list = AnonymousAnswer.objects.filter(session_key = session_key)
    #from askbot.conf import settings as askbot_settings
    if askbot_settings.EMAIL_VALIDATION == True:#add user to the record
        for aq in aq_list:
            aq.author = user
            aq.save()
        for aa in aa_list:
            aa.author = user
            aa.save()
        #maybe add pending posts message?
    else:
        if user.is_blocked():
            msg = _('blocked users cannot post')
            user.message_set.create(message = msg)
        elif user.is_suspended():
            msg = _('suspended users cannot post')
            user.message_set.create(message = msg)
        else:
            for aq in aq_list:
                aq.publish(user)
            for aa in aa_list:
                aa.publish(user)

#signal for User model save changes
django_signals.pre_save.connect(calculate_gravatar_hash, sender=User)
django_signals.post_save.connect(record_award_event, sender=Award)
django_signals.post_save.connect(notify_award_message, sender=Award)
django_signals.post_save.connect(record_answer_accepted, sender=Answer)
django_signals.post_save.connect(record_vote, sender=Vote)
django_signals.post_save.connect(
                            record_favorite_question,
                            sender=FavoriteQuestion
                        )
django_signals.post_delete.connect(record_cancel_vote, sender=Vote)

#change this to real m2m_changed with Django1.2
signals.delete_question_or_answer.connect(record_delete_question, sender=Question)
signals.delete_question_or_answer.connect(record_delete_question, sender=Answer)
signals.flag_offensive.connect(record_flag_offensive, sender=Question)
signals.flag_offensive.connect(record_flag_offensive, sender=Answer)
signals.tags_updated.connect(record_update_tags)
signals.user_updated.connect(record_user_full_updated, sender=User)
signals.user_logged_in.connect(post_stored_anonymous_content)
signals.post_updated.connect(
                           record_post_update_activity,
                           sender=Comment
                       )
signals.post_updated.connect(
                           record_post_update_activity,
                           sender=Answer
                       )
signals.post_updated.connect(
                           record_post_update_activity,
                           sender=Question
                       )
signals.site_visited.connect(record_user_visit)

#todo: wtf??? what is x=x about?
signals = signals

Question = Question
QuestionRevision = QuestionRevision
QuestionView = QuestionView
FavoriteQuestion = FavoriteQuestion
AnonymousQuestion = AnonymousQuestion

Answer = Answer
AnswerRevision = AnswerRevision
AnonymousAnswer = AnonymousAnswer

Tag = Tag
Comment = Comment
Vote = Vote
MarkedTag = MarkedTag

BadgeData = BadgeData
Award = Award
Repute = Repute

Activity = Activity
ActivityAuditStatus = ActivityAuditStatus
EmailFeedSetting = EmailFeedSetting
#AuthKeyUserAssociation = AuthKeyUserAssociation

__all__ = [
        'signals',

        'Question',
        'QuestionRevision',
        'QuestionView',
        'FavoriteQuestion',
        'AnonymousQuestion',

        'Answer',
        'AnswerRevision',
        'AnonymousAnswer',

        'Tag',
        'Comment',
        'Vote',
        'MarkedTag',

        'BadgeData',
        'Award',
        'Repute',

        'Activity',
        'ActivityAuditStatus',
        'EmailFeedSetting',
        #'AuthKeyUserAssociation',

        'User',

        'get_model'
]
