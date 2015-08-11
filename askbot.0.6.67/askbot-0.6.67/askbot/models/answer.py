import datetime
from django.db import models
from django.utils.http import urlquote  as django_urlquote
from django.utils.translation import ugettext as _
from django.core.urlresolvers import reverse
from django.core import exceptions as django_exceptions
from django.conf import settings
from askbot import exceptions
from askbot.models.base import AnonymousContent, DeletableContent
from askbot.models.base import ContentRevision
from askbot.models.base import parse_post_text, parse_and_save_post
from askbot.models import content
from askbot import const
from askbot.utils.slug import slugify
from askbot.utils import markup
from askbot.utils.html import sanitize_html

class AnswerManager(models.Manager):
    def create_new(
                self, 
                question=None, 
                author=None, 
                added_at=None, 
                wiki=False, 
                text='', 
                email_notify=False
            ):

        answer = Answer(
            question = question,
            author = author,
            added_at = added_at,
            wiki = wiki,
            text = text,
            #.html field is denormalized by the save() call
        )
        if answer.wiki:
            answer.last_edited_by = answer.author
            answer.last_edited_at = added_at
            answer.wikified_at = added_at

        answer.parse_and_save(author = author)

        answer.add_revision(
            author = author,
            revised_at = added_at,
            text = text,
            comment = const.POST_STATUS['default_version'],
        )

        #update question data
        question.last_activity_at = added_at
        question.last_activity_by = author
        question.answer_count +=1
        question.save()

        #set notification/delete
        if email_notify:
            if author not in question.followed_by.all():
                question.followed_by.add(author)
        else:
            #not sure if this is necessary. ajax should take care of this...
            try:
                question.followed_by.remove(author)
            except:
                pass
        return answer

    def get_author_list(self, **kwargs):
        authors = set()
        for answer in self:
            authors.update(answer.get_author_list(**kwargs))
        return list(authors)

    #todo: I think this method is not being used anymore, I'll just comment it for now
    #def get_answers_from_questions(self, user_id):
    #    """
    #    Retrieves visibile answers for the given question. Which are not included own answers
    #    """
    #    cursor = connection.cursor()
    #    cursor.execute(self.GET_ANSWERS_FROM_USER_QUESTIONS, [user_id, user_id])
    #    return cursor.fetchall()

class Answer(content.Content, DeletableContent):
    post_type = 'answer'
    question = models.ForeignKey('Question', related_name='answers')
    accepted    = models.BooleanField(default=False)
    accepted_at = models.DateTimeField(null=True, blank=True)

    objects = AnswerManager()

    class Meta(content.Content.Meta):
        db_table = u'answer'

    parse = parse_post_text
    parse_and_save = parse_and_save_post

    def assert_is_visible_to(self, user):
        """raises QuestionHidden or AnswerHidden"""
        try:
            self.question.assert_is_visible_to(user)
        except exceptions.QuestionHidden:
            message = _(
                        'Sorry, the answer you are looking for is '
                        'no longer available, because the parent '
                        'question has been removed'
                       )
            raise exceptions.QuestionHidden(message)
        if self.deleted:
            message = _(
                    'Sorry, this answer has been '
                    'removed and is no longer accessible'
                )
            if user.is_anonymous():
                raise exceptions.AnswerHidden(message)
            try:
                user.assert_can_see_deleted_post(self)
            except django_exceptions.PermissionDenied:
                raise exceptions.AnswerHidden(message)

    def get_updated_activity_data(self, created = False):
        #todo: simplify this to always return latest revision for the second
        #part
        if created:
            return const.TYPE_ACTIVITY_ANSWER, self
        else:
            latest_revision = self.get_latest_revision()
            return const.TYPE_ACTIVITY_UPDATE_ANSWER, latest_revision

    def apply_edit(self, edited_at=None, edited_by=None, text=None, comment=None, wiki=False):

        if text is None:
            text = self.get_latest_revision().text
        if edited_at is None:
            edited_at = datetime.datetime.now()
        if edited_by is None:
            raise Exception('edited_by is required')

        self.last_edited_at = edited_at
        self.last_edited_by = edited_by
        #self.html is denormalized in save()
        self.text = text
        #todo: bug wiki has no effect here

        #must add revision before saving the answer
        self.add_revision(
            author = edited_by,
            revised_at = edited_at,
            text = text,
            comment = comment
        )

        self.parse_and_save(author = edited_by)

        self.question.last_activity_at = edited_at
        self.question.last_activity_by = edited_by
        self.question.save()

    def add_revision(self, author=None, revised_at=None, text=None, comment=None):
        #todo: this may be identical to Question.add_revision
        if None in (author, revised_at, text):
            raise Exception('arguments author, revised_at and text are required')
        rev_no = self.revisions.all().count() + 1
        if comment in (None, ''):
            if rev_no == 1:
                comment = const.POST_STATUS['default_version']
            else:
                comment = 'No.%s Revision' % rev_no
        return AnswerRevision.objects.create(
                                  answer=self,
                                  author=author,
                                  revised_at=revised_at,
                                  text=text,
                                  summary=comment,
                                  revision=rev_no
                                  )

    def save(self, *args, **kwargs):
        super(Answer, self).save(*args, **kwargs);
        if 'postgres' in settings.DATABASE_ENGINE:
            #hit the database to trigger update of full text search vector
            self.question.save()

    def get_origin_post(self):
        return self.question

    def get_page_number(self, answers = None):
        """When question has many answers, answers are
        paginated. This function returns number of the page
        on which the answer will be shown, using the default
        sort order. The result may depend on the visitor."""
        order_number = 0
        for answer in answers:
            if self == answer:
                break
            order_number += 1
        return int(order_number/const.ANSWERS_PAGE_SIZE) + 1

    def get_response_receivers(self, exclude_list = None):
        """get list of users interested in this response
        update based on their participation in the question
        activity

        exclude_list is required and normally should contain
        author of the updated so that he/she is not notified of
        the response
        """
        assert(exclude_list is not None)
        recipients = set()
        recipients.update(
                            self.get_author_list(
                                include_comments = True 
                            )
                        )
        recipients.update(
                            self.question.get_author_list(
                                    include_comments = True
                                )
                        )
        for answer in self.question.answers.all():
            recipients.update(answer.get_author_list())

        recipients -= set(exclude_list)

        return list(recipients)

    def get_user_vote(self, user):
        if user.is_anonymous():
            return None

        votes = self.votes.filter(user=user)
        if votes and votes.count() > 0:
            return votes[0]
        else:
            return None

    def get_question_title(self):
        return self.question.title

    def get_absolute_url(self):
        return '%(base)s%(slug)s?answer=%(id)d#%(id)d' % \
                {
                    'base': reverse('question', args=[self.question.id]),
                    'slug': django_urlquote(slugify(self.question.title)),
                    'id': self.id
                }

    def __unicode__(self):
        return self.html
        

class AnswerRevision(ContentRevision):
    """A revision of an Answer."""
    answer     = models.ForeignKey('Answer', related_name='revisions')

    def get_absolute_url(self):
        return reverse('answer_revisions', kwargs={'id':self.answer.id})

    def get_question_title(self):
        return self.answer.question.title

    def as_html(self):
        markdowner = markup.get_parser()
        return sanitize_html(markdowner.convert(self.text))

    class Meta(ContentRevision.Meta):
        db_table = u'answer_revision'
        ordering = ('-revision',)

    def save(self, **kwargs):
        """Looks up the next available revision number if not set."""
        if not self.revision:
            self.revision = AnswerRevision.objects.filter(
                answer=self.answer).values_list('revision',
                                                flat=True)[0] + 1
        super(AnswerRevision, self).save(**kwargs)

class AnonymousAnswer(AnonymousContent):
    question = models.ForeignKey('Question', related_name='anonymous_answers')

    def publish(self,user):
        added_at = datetime.datetime.now()
        Answer.objects.create_new(question=self.question,wiki=self.wiki,
                            added_at=added_at,text=self.text,
                            author=user)
        self.delete()
